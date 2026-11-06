#!/usr/bin/env python3
"""Собрать stable_sync.py.new: новый код + конфиг (PARENT_URL, API_KEY, DB_CONFIG) из живого файла.

usage: build_new.py LIVE NEW OUT VERSION
Инварианты (иначе ничего не пишет, код 1): каждая область находится ровно один раз в обоих файлах;
в собранном файле области совпадают с живым, а ВСЁ ОСТАЛЬНОЕ байт-в-байт равно NEW (проверяется
обратной подстановкой областей NEW); есть VERSION = '<VERSION>'; нет плейсхолдеров. Значения не печатает.
"""
import os
import re
import sys

LIVE, NEW, OUT, VERSION = sys.argv[1:5]
# Однострочные — только re.M (без re.S: .* не должен переходить строку). DB_CONFIG — от «DB_CONFIG = {»
# до первой «}», стоящей в начале строки; вложенных {} быть не должно.
REGIONS = (('PARENT_URL', r'^PARENT_URL = [^\n]*$'),
           ('API_KEY', r'^API_KEY = [^\n]*$'),
           ('DB_CONFIG', r'^DB_CONFIG = \{[^{}]*^\}$'))
PLACEHOLDERS = ('<MASKED', '<PLACEHOLDER', 'your-api-key-here', 'your-parent-panel')


def region(text, pat, what):
    m = list(re.finditer(pat, text, re.M))
    if len(m) != 1:
        sys.exit(f'{what}: найдено {len(m)} — сборка отменена')
    return m[0]


def splice(text, pat, value, what):
    m = region(text, pat, what)
    return text[:m.start()] + value + text[m.end():]


live = open(LIVE, encoding='utf-8').read()
src = open(NEW, encoding='utf-8').read()
built = src
for key, pat in REGIONS:
    built = splice(built, pat, region(live, pat, key + ' (живой)').group(0), key + ' (новый)')

back = built
for key, pat in REGIONS:
    if region(built, pat, key + ' (собранный)').group(0) != region(live, pat, key).group(0):
        sys.exit(f'{key}: в собранном не равен живому — сборка отменена')
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
print(f'{os.path.basename(OUT)} собран: v{VERSION}, конфиг (PARENT_URL, API_KEY, DB_CONFIG) из живого файла, '
      f'остальное байт-в-байт = новый код ({len(src)} Б → {len(built)} Б)')
