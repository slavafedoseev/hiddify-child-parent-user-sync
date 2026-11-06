#!/usr/bin/env python3
"""Собрать stable_sync.py.new: новый код + конфиг (PARENT_URL, API_KEY, DB_CONFIG, PARENT_TLS_VERIFY)
из живого файла.

usage: build_new.py LIVE NEW OUT VERSION
Инварианты (иначе ничего не пишет, код 1): обязательная область находится ровно один раз в обоих
файлах, необязательная (PARENT_TLS_VERIFY) — ровно один раз в NEW и не больше одного раза в живом (нет в
живом — остаётся значение NEW); в собранном файле области совпадают с живым, а ВСЁ ОСТАЛЬНОЕ байт-в-байт
равно NEW (проверяется обратной подстановкой областей NEW); есть VERSION = '<VERSION>'; нет
плейсхолдеров. Значения не печатает.
"""
import os
import re
import sys

LIVE, NEW, OUT, VERSION = sys.argv[1:5]
# Однострочные — только re.M (без re.S: .* не должен переходить строку). DB_CONFIG — от «DB_CONFIG = {»
# до первой «}», стоящей в начале строки; вложенных {} быть не должно.
REGIONS = (('PARENT_URL', r'^PARENT_URL = [^\n]*$', True),
           ('API_KEY', r'^API_KEY = [^\n]*$', True),
           ('DB_CONFIG', r'^DB_CONFIG = \{[^{}]*^\}$', True),
           ('PARENT_TLS_VERIFY', r'^PARENT_TLS_VERIFY = [^\n]*$', False))
PLACEHOLDERS = ('<MASKED', '<PLACEHOLDER', 'your-api-key-here', 'your-parent-panel')


def region(text, pat, what, optional=False):
    m = list(re.finditer(pat, text, re.M))
    if optional and not m:
        return None
    if len(m) != 1:
        sys.exit(f'{what}: найдено {len(m)} — сборка отменена')
    return m[0]


def splice(text, pat, value, what):
    m = region(text, pat, what)
    return text[:m.start()] + value + text[m.end():]


live = open(LIVE, encoding='utf-8').read()
src = open(NEW, encoding='utf-8').read()
# Значение каждой области: из живого файла; необязательной, которой там нет, — из нового кода.
want = {}
for key, pat, required in REGIONS:
    region(src, pat, key + ' (новый)')
    m = region(live, pat, key + ' (живой)', optional=not required)
    want[key] = m.group(0) if m else region(src, pat, key).group(0)
built = src
for key, pat, required in REGIONS:
    built = splice(built, pat, want[key], key + ' (новый)')

back = built
for key, pat, required in REGIONS:
    if region(built, pat, key + ' (собранный)').group(0) != want[key]:
        sys.exit(f'{key}: в собранном не то значение — сборка отменена')
    back = splice(back, pat, region(src, pat, key).group(0), key + ' (обратно)')
if back != src:
    sys.exit('кроме конфига собранный файл отличается от нового кода — сборка отменена')
if f"VERSION = '{VERSION}'" not in built:
    sys.exit(f'в собранном нет VERSION = {VERSION!r} — сборка отменена')
for p in PLACEHOLDERS:
    if p in built:
        sys.exit(f'в собранном остался плейсхолдер {p} — сборка отменена')

fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(built)
kept = [k for k, pat, required in REGIONS if not required and not region(live, pat, k, optional=True)]
print(f'{os.path.basename(OUT)} собран: v{VERSION}, конфиг из живого файла'
      + (f' (по умолчанию нового кода: {", ".join(kept)})' if kept else '')
      + f', остальное байт-в-байт = новый код ({len(src)} Б → {len(built)} Б)')
