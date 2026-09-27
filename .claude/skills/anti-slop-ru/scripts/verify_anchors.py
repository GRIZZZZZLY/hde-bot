#!/usr/bin/env python3
import argparse, json, re
from pathlib import Path

RX = re.compile(r"https?://\S+|\b[\w.+-]+@[\w.-]+\.\w+\b|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\bv?\d+(?:\.\d+){1,4}\b|\b\d+(?:[.,]\d+)?\s*(?:%|₽|руб\.?|USD|EUR|ГБ|МБ|GB|MB|кг|мг|мм|см|м|ч|мин)\b|\b[A-ZА-ЯЁ]{2,}[A-ZА-ЯЁ0-9_-]*\d+[A-ZА-ЯЁ0-9_.-]*\b", re.I)

def anchors(text): return set(m.group(0) for m in RX.finditer(text))
def compare(a,b):
    x,y=anchors(a),anchors(b)
    return {'removed':sorted(x-y),'added':sorted(y-x),'source_count':len(x),'rewrite_count':len(y)}
def self_test():
    r=compare('Версия v2.5 стоит 100 ₽ до 12.10.2026.','Версия v2.6 стоит 100 ₽ до 12.10.2026.')
    assert 'v2.5' in r['removed'] and 'v2.6' in r['added']; print('OK')
def main():
    p=argparse.ArgumentParser(); p.add_argument('source',nargs='?'); p.add_argument('rewrite',nargs='?'); p.add_argument('--self-test',action='store_true'); a=p.parse_args()
    if a.self_test: self_test(); return
    if not a.source or not a.rewrite: p.error('source and rewrite files are required')
    r=compare(Path(a.source).read_text(encoding='utf-8'),Path(a.rewrite).read_text(encoding='utf-8'))
    print(json.dumps(r,ensure_ascii=False,indent=2))
if __name__=='__main__': main()