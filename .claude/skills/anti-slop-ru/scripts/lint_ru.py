#!/usr/bin/env python3
import argparse, json, re, sys
from pathlib import Path

HARD = [
    ("artifact_citation", re.compile(r"(?:turn\d+(?:search|view|news|fetch|file)\d+|utm_source=chatgpt\.com|\[cite:\s*\d+\]|cite)", re.I)),
]
SOFT = [
    ("meta_preamble", re.compile(r"^(?:конечно[!,.]?|разумеется[!,.]?|давайте\s+разбер[её]мся|вот\s+(?:улучшенная|переписанная)\s+версия)", re.I)),
    ("empty_importance", re.compile(r"\b(?:важно|следует)\s+отметить\b|\bиграет\s+(?:ключевую|важную)\s+роль\b|\bнеотъемлем(?:ая|ой|ую|ым)\s+част", re.I)),
    ("generic_opening", re.compile(r"\b(?:в\s+современном\s+мире|в\s+условиях\s+стремительного\s+развития)\b", re.I)),
    ("binary_frame", re.compile(r"\bне\s+просто\b.{0,90}\bа\b", re.I|re.S)),
    ("support_conveyor", re.compile(r"спасибо за обращение|приносим\s+(?:свои\s+)?извинения|в\s+кратчайшие\s+сроки|на\s+(?:текущий|данный)\s+момент|данный\s+вопрос|уважаем\w+\s+(?:клиент|пользовател)|информируем\s+вас|должна\s+быть\s+решена", re.I)),
]

def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]

def scan(text):
    findings=[]
    for severity, rules in (("hard",HARD),("soft",SOFT)):
        for name, rx in rules:
            for m in rx.finditer(text):
                findings.append({"severity":severity,"rule":name,"start":m.start(),"excerpt":m.group(0)[:120]})
    ss=sentences(text)
    if len(ss)>=8:
        lens=[len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+",s)) for s in ss]
        mean=sum(lens)/len(lens)
        if mean:
            var=sum((x-mean)**2 for x in lens)/len(lens)
            cv=(var**0.5)/mean
            if cv<0.22:
                findings.append({"severity":"soft","rule":"sentence_length_metronome","value":round(cv,3),"excerpt":"low sentence-length variation"})
    xeto=len(re.findall(r"\b[^\n.!?]{1,50}\s—\sэто\s",text,re.I))
    if xeto>=3:
        findings.append({"severity":"soft","rule":"repeated_dash_eto","value":xeto,"excerpt":"repeated X — это Y construction"})
    return findings

def self_test():
    a=scan("Важно отметить, что это полезно. turn0search1")
    assert any(x['rule']=='artifact_citation' for x in a)
    assert any(x['rule']=='empty_importance' for x in a)
    b=scan("Москва — столица России. Это нормальное русское тире.")
    assert not any(x['rule']=='repeated_dash_eto' for x in b)
    c=scan("Спасибо за обращение. Информируем вас, что проблема должна быть решена.")
    assert any(x['rule']=='support_conveyor' for x in c)
    d=scan("Понимаю, смену надо закрыть сейчас. Переключите кабель в другой разъём. Получилось?")
    assert not any(x['rule']=='support_conveyor' for x in d)
    print("OK")

def main():
    p=argparse.ArgumentParser()
    p.add_argument('file',nargs='?')
    p.add_argument('--json',action='store_true')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    if a.self_test: self_test(); return
    if not a.file: p.error('file is required')
    text=Path(a.file).read_text(encoding='utf-8')
    out=scan(text)
    if a.json: print(json.dumps(out,ensure_ascii=False,indent=2))
    else:
        for x in out: print(f"{x['severity'].upper()} {x['rule']}: {x.get('excerpt','')}")
        print(f"Findings: {len(out)}")
    sys.exit(1 if any(x['severity']=='hard' for x in out) else 0)
if __name__=='__main__': main()