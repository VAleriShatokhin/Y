#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Поиск карточки дела на сайте суда (ГАС «Правосудие», модуль sud_delo)
и извлечение даты/времени судебного заседания.

Капча решается через сервис RuCaptcha / 2captcha (совместимый API).

Пример:
    export RUCAPTCHA_KEY=ваш_ключ
    python sudrf_case_lookup.py \
        --court https://mitishy--mo.sudrf.ru \
        --case "13-298/2026"

Зависимости:
    pip install playwright requests
    playwright install chromium

ВАЖНО про окружение Claude Code on the web:
    Домен sudrf.ru по умолчанию заблокирован сетевой политикой (white-list).
    Чтобы скрипт смог достучаться до суда, нужно расширить network policy
    окружения (добавить *.sudrf.ru). См.
    https://code.claude.com/docs/en/claude-code-on-the-web
"""

import argparse
import os
import sys
import time
import re

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Нужен Playwright:  pip install playwright requests && playwright install chromium",
          file=sys.stderr)
    sys.exit(1)


RUCAPTCHA_IN = "https://rucaptcha.com/in.php"
RUCAPTCHA_RES = "https://rucaptcha.com/res.php"


def solve_image_captcha(api_key: str, image_bytes: bytes, timeout: int = 120) -> str:
    """Отправляет картинку капчи в RuCaptcha и ждёт текстовый ответ."""
    resp = requests.post(
        RUCAPTCHA_IN,
        data={"key": api_key, "method": "base64", "json": 1, "numeric": 0},
        files={"file": ("captcha.png", image_bytes)},
        timeout=30,
    ).json()
    if resp.get("status") != 1:
        raise RuntimeError(f"RuCaptcha in.php error: {resp}")
    captcha_id = resp["request"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        r = requests.get(
            RUCAPTCHA_RES,
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        ).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"RuCaptcha res.php error: {r}")
    raise TimeoutError("RuCaptcha не успел решить капчу за отведённое время")


def lookup(court_url: str, case_number: str, api_key: str, headless: bool = True) -> dict:
    """Открывает модуль поиска, решает капчу, ищет дело, возвращает поля карточки."""
    court_url = court_url.rstrip("/")
    result = {"case_number": case_number, "court": court_url}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            locale="ru-RU",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
        )
        page = ctx.new_page()

        # 1. Открываем вкладку «Поиск информации по делам» модуля sud_delo.
        search_url = (f"{court_url}/modules.php?name=sud_delo&srv_num=1"
                      f"&name_op=sf&delo_id=1540005")
        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

        # 2. Вводим номер дела. Поле в разных версиях называется по-разному —
        #    пробуем несколько распространённых вариантов.
        case_selectors = [
            "input#case_number",
            "input[name='case__case_numberss']",
            "input[name='_deloId']",
            "input[name='delo_table_case__case_numberss']",
        ]
        filled = False
        for sel in case_selectors:
            try:
                page.fill(sel, case_number, timeout=3000)
                filled = True
                break
            except PWTimeout:
                continue
        if not filled:
            # запасной путь — первое текстовое поле формы поиска
            page.fill("form input[type='text']", case_number, timeout=5000)

        # 3. Капча: находим <img> капчи, делаем скриншот, решаем.
        captcha_img = None
        for sel in ["img#captcha", "img[src*='captcha']", "img[src*='Captcha']"]:
            el = page.query_selector(sel)
            if el:
                captcha_img = el
                break
        if captcha_img:
            png = captcha_img.screenshot()
            answer = solve_image_captcha(api_key, png)
            for sel in ["input#captcha", "input[name='captcha']",
                        "input[name='captchaCode']"]:
                try:
                    page.fill(sel, answer, timeout=2000)
                    break
                except PWTimeout:
                    continue

        # 4. Отправляем форму.
        for sel in ["input[type='submit']", "button[type='submit']",
                    "input[value*='Найти']", "input[value*='айти']"]:
            el = page.query_selector(sel)
            if el:
                el.click()
                break
        page.wait_for_load_state("networkidle", timeout=60000)

        # 5. В таблице результатов ищем ссылку на дело и открываем карточку.
        link = page.query_selector(f"a:has-text('{case_number}')")
        if link:
            link.click()
            page.wait_for_load_state("networkidle", timeout=60000)

        # 6. Достаём дату заседания из текста карточки.
        body = page.inner_text("body")
        m = re.search(
            r"(?:Дата(?:\s+и\s+время)?\s+(?:судебного\s+)?заседани[яе])\D{0,40}"
            r"(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?)",
            body, re.IGNORECASE)
        result["hearing_datetime"] = m.group(1) if m else None
        result["found"] = bool(m)
        result["raw_excerpt"] = body[:2000]

        browser.close()
    return result


def main():
    ap = argparse.ArgumentParser(description="Поиск даты заседания по номеру дела на сайте суда")
    ap.add_argument("--court", required=True, help="Базовый URL суда, напр. https://mitishy--mo.sudrf.ru")
    ap.add_argument("--case", required=True, help="Номер дела, напр. 13-298/2026")
    ap.add_argument("--key", default=os.environ.get("RUCAPTCHA_KEY"),
                    help="Ключ RuCaptcha/2captcha (или env RUCAPTCHA_KEY)")
    ap.add_argument("--show", action="store_true", help="Запустить браузер видимым (headful)")
    args = ap.parse_args()

    if not args.key:
        print("Нужен ключ RuCaptcha: --key или env RUCAPTCHA_KEY", file=sys.stderr)
        sys.exit(2)

    res = lookup(args.court, args.case, args.key, headless=not args.show)
    print("\n=== Результат ===")
    print(f"Суд:    {res['court']}")
    print(f"Дело:   {res['case_number']}")
    if res.get("found"):
        print(f"Заседание: {res['hearing_datetime']}")
    else:
        print("Дата заседания не найдена. Фрагмент карточки для проверки селекторов:")
        print(res.get("raw_excerpt", "")[:800])


if __name__ == "__main__":
    main()
