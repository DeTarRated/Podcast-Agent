#!/usr/bin/env python3
"""领域知识库工具。知识库各文件的格式见 ../references/kb_format.md,沉淀规则见 ../references/sediment_rules.md。

知识库放在 skill 目录之外(skill 是安装拷贝,会被更新覆盖;仓库又是公开的),位置这样解析:
  $TRANSLATE_SRT_HOME  ->  缺省 ~/.translate-srt/
      config.json          knowledge_path / default_target_lang / review_mode / volatile_recheck_months
      knowledge/           缺省知识库位置(config 里可改成别处)

用法:
  python kb_tools.py init [--home DIR]                 # 建 home + config + 空知识库骨架,并 git init
  python kb_tools.py status                            # 打印解析出来的路径、各领域条目数、git 状态
  python kb_tools.py check                             # 校验知识库格式(列数、重复、短别名、别名是 canonical 子串等)
  python kb_tools.py match in.srt [-o hits.json]       # SRT × 知识库:命中清单、领域建议、未覆盖的片假名/拉丁词候选
  python kb_tools.py replace in.srt [-o out.srt]       # 把 mode=auto 的 asr_variants 换成 canonical;ask 的只报位置
  python kb_tools.py remap-log in.srt --log log.tsv    # 整平后按时间戳更新日志中的 current_entries
  python kb_tools.py glossary in.srt -o glossary.md    # 从命中条目生成本次专用术语表(可 -d 指定领域、--video-date 判 volatile)
  python kb_tools.py backfill DIR... -o staging.md     # 扫历史项目的 _context/,汇总成沉淀素材
  python kb_tools.py apply proposal.json [--commit]    # 把沉淀提案合并进知识库;译法冲突不覆盖、单列 CONFLICT
  python kb_tools.py diff                              # 知识库的 git diff --stat + 未提交改动
  python kb_tools.py --self-test
"""
import argparse
import csv
import datetime as dt
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from srt_tools import parse, serialize, _split_speaker  # noqa: E402

ALIAS_COLUMNS = ["canonical", "asr_variants", "translation", "domain", "type", "mode", "notes"]
ALIAS_TYPES = {"person", "character", "work", "show", "team", "term", "org", "place", "event", "nickname"}
ALIAS_MODES = {"auto", "ask"}
ALIAS_LOG_COLUMNS = ["entry", "variant", "canonical", "status", "start_ms", "end_ms",
                     "source_text", "current_entries"]
DOMAIN_FILES = ["entities.md", "glossary.md", "style.md", "sources.md"]
# 别名短于这个显示长度就不该 auto:日语子串匹配没有词边界,两三个字的片假名/汉字到处撞
AUTO_MIN_LEN = 3
# 短于这个长度的写法不参与 match 的命中统计(单字必然误命中)
MATCH_MIN_LEN = 2
KATAKANA_RUN = re.compile(r"[ァ-ヶー]{3,}")
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'&.\-]{2,}")
STABILITY_RE = re.compile(r"^(stable|volatile\((\d{4})-(\d{2})\))$")
# 缺口候选的停用词:常见外来语,不是专名。知识库根目录的 stopwords.txt(一行一个)会追加进来
KATAKANA_STOPWORDS = frozenset("""
アイドル アイテム アイス アカウント アクセス アップ アドバイス アナウンス アニメ アプリ アルバム アンケート イベント イメージ イラスト
インタビュー インターネット エピソード エンディング オープニング オーディション オススメ オタク カード カフェ カバー カメラ カラー カレンダー
キャラ キャラクター キャンペーン キャンセル クイズ クッキー クラス グッズ グループ ゲーム ゲスト コーナー コーヒー コメント コラボ コンビニ
コンテンツ サイズ サイン サポート サービス シーン システム シャツ ショップ シリーズ シングル シーズン スタジオ スタッフ スタート ステージ
ステッカー ストーリー スマホ スケジュール セット セリフ ソング タイトル タイミング タイプ ダウンロード チーム チェック チケット チャンス
チャレンジ データ デザイン テーマ テスト テレビ テンション トーク トラブル ドラマ ドキドキ ネタ ネタバレ ニュース パート パーティー
バージョン バッジ バランス パターン パソコン ハッピーエンド バッドエンド バラエティ ビデオ ファン ファンレター フォロー プレゼント
プレイ プロデューサー ページ ベスト ポイント ボイス ホテル マイク マネージャー ミュート メイク メール メンバー メンション メッセージ
ライブ ラジオ ラジオネーム ランキング リアル リクエスト リスナー リハーサル リポーター リメイク リリース レッスン レベル ロケ
ワード ワンピース ユニット ユーザー ジャンル ジャケット サイト リンク ルール ボタン パンツ スカート ヘアー ヘア ハート ハッシュタグ
タグ フリル ホール アリーナ ツアー フェス フェスティバル ドーム コーラス ダンス レコーディング アフレコ ナレーション キャスト
マンガ ノベル ラノベ ゲット バイト カット シーツ ブランド メニュー ランチ ディナー デザート ドリンク ケーキ アイスクリーム
""".split())
SEP_RE = re.compile(r"\s*[;；/]\s*")

DEFAULT_CONFIG = {
    "knowledge_path": "knowledge",
    "default_target_lang": "zh",
    "review_mode": "full",
    "volatile_recheck_months": 3,
}

INDEX_TEMPLATE = """# 知识库索引

主代理每次翻译前必读。每个领域一段,几行说清「这个领域是什么、看到哪些词该加载它、常驻人物有谁」。
领域包在同名子目录下:`entities.md`(人物/作品档案)、`glossary.md`(术语与译法)、`style.md`(翻译约定)、`sources.md`(查过的 URL)。
全局别名表 `aliases.tsv` 供脚本做 ASR 纠错与命中统计。

## seiyuu
日本女声优相关:广播节目、活动、生放送、访谈。
关键词:声優 / ラジオ / 収録 / アフレコ / イベント / 生放送 / メール / ふつおた
常驻人物:(待填)
"""

ENTITIES_TEMPLATE = """# {domain} · 人物/作品档案

格式见 kb_format.md。每个条目一个 `### 原文正确写法` 小节,字段用 `- 键: 值`。

"""
GLOSSARY_TEMPLATE = """# {domain} · 术语与译法

格式见 kb_format.md。每个条目一个 `### 原文` 小节(多种写法用 ` / ` 分隔),字段用 `- 键: 值`。

"""
STYLE_TEMPLATE = """# {domain} · 翻译约定

不是术语,是处理方式:敬称怎么译、语气词保不保留、梗和内部笑话的惯例、口癖怎么对应。
只收用户确认过、或多次出现的偏好;一次性的润色不进这里。

## 敬称与称呼

## 语气与口语

## 节目惯例

## 其他
"""
SOURCES_TEMPLATE = """# {domain} · 有用过的来源

每行一条:`- URL — 用途`。调研子代理先 fetch 这里的,命中不了再 search。

"""


# ---------------------------------------------------------------- config / paths

def home_dir():
    env = os.environ.get("TRANSLATE_SRT_HOME")
    return Path(env).expanduser().resolve() if env else (Path.home() / ".translate-srt").resolve()


def load_config(home=None):
    home = home or home_dir()
    cfg = dict(DEFAULT_CONFIG)
    path = home / "config.json"
    if path.is_file():
        cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return cfg


def kb_path(home=None, cfg=None):
    home = home or home_dir()
    cfg = cfg or load_config(home)
    p = Path(os.path.expanduser(cfg["knowledge_path"]))
    return (p if p.is_absolute() else home / p).resolve()


def require_kb(home=None):
    kb = kb_path(home)
    if not (kb / "aliases.tsv").is_file():
        sys.exit(f"ERROR: knowledge base not found at {kb} (run `kb_tools.py init` first, "
                 f"or set TRANSLATE_SRT_HOME / config.json knowledge_path)")
    return kb


def domains_of(kb):
    return sorted(p.name for p in kb.iterdir() if p.is_dir() and not p.name.startswith(".") and (p / "entities.md").exists())


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read(path):
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def _git(kb, *args, check=False):
    try:
        r = subprocess.run(["git", "-C", str(kb), *args], capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        return None
    if check and r.returncode != 0:
        sys.exit(f"ERROR: git {' '.join(args)} failed:\n{r.stderr}")
    return r


# ---------------------------------------------------------------- knowledge base parsing

def load_aliases(kb):
    rows = []
    path = kb / "aliases.tsv"
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if reader.fieldnames != ALIAS_COLUMNS:
            sys.exit(f"ERROR: {path} header must be {ALIAS_COLUMNS}, got {reader.fieldnames}")
        for i, r in enumerate(reader, 2):
            r = {k: (v or "").strip() for k, v in r.items()}
            if not r["canonical"]:
                continue
            r["_line"] = i
            r["variants"] = [v for v in SEP_RE.split(r["asr_variants"]) if v]
            rows.append(r)
    return rows


def save_aliases(kb, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(ALIAS_COLUMNS)
    for r in sorted(rows, key=lambda r: (r["domain"], r["type"], r["canonical"])):
        w.writerow([r["canonical"], ";".join(r["variants"]), r["translation"], r["domain"],
                    r["type"], r["mode"], r["notes"]])
    _write(kb / "aliases.tsv", buf.getvalue())


def parse_sections(text):
    """`### 标题` 分节的 Markdown -> [{name, fields{key:value}, lines[原文行], start, end}]。

    字段行形如 `- 键: 值`(冒号全半角都认);其他行原样保留在 lines 里。
    """
    sections, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^### +(.+?)\s*$", line)
        if m:
            cur = {"name": m.group(1), "fields": {}, "lines": [], "order": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        fm = re.match(r"^- +([^:：]+?)\s*[:：]\s*(.*)$", line)
        if fm and fm.group(1) not in cur["fields"]:
            cur["fields"][fm.group(1)] = fm.group(2).strip()
            cur["order"].append(fm.group(1))
    return sections


def render_section(sec, level=3):
    lines = [f"{'#' * level} {sec['name']}"]
    lines.extend(sec["lines"])
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n\n"


def section_names(sec):
    """一个条目的全部可匹配写法:标题(按 / 拆)+ 别名字段。"""
    names = [n for n in SEP_RE.split(sec["name"]) if n]
    for key in ("别名", "aliases"):
        if key in sec["fields"]:
            names.extend(n for n in SEP_RE.split(sec["fields"][key]) if n)
    # 去掉别名后面的括号注记:`鈴本あきほ(ASR)` -> `鈴本あきほ`
    return [re.sub(r"[(（][^()（）]*[)）]$", "", n).strip() for n in names if n.strip()]


def stability_state(value, video_date, months):
    """返回 'stable' / 'fresh' / 'stale' / 'unknown'。"""
    if not value:
        return "unknown"
    m = STABILITY_RE.match(value.strip())
    if not m:
        return "unknown"
    if m.group(1) == "stable":
        return "stable"
    y, mo = int(m.group(2)), int(m.group(3))
    marked = dt.date(y, mo, 1)
    limit_month = mo + months
    limit = dt.date(y + (limit_month - 1) // 12, (limit_month - 1) % 12 + 1, 1)
    return "stale" if video_date >= limit else "fresh"


def load_domain(kb, domain):
    d = kb / domain
    return {
        "name": domain,
        "entities": parse_sections(_read(d / "entities.md")),
        "glossary": parse_sections(_read(d / "glossary.md")),
        "style": _read(d / "style.md"),
        "sources": _read(d / "sources.md"),
    }


def _known_translation(value):
    value = (value or "").strip()
    return "" if value == "(未定)" else value


def _identity_names(name):
    # Canonical/title variants identify an entity; nicknames need not be unique.
    return {name.strip()} | {part for part in SEP_RE.split(name.strip()) if part}


def _translation_records(kb):
    for row in load_aliases(kb):
        translation = _known_translation(row["translation"])
        if translation:
            yield (row["domain"], {row["canonical"]}, translation,
                   f"aliases.tsv:{row['_line']} {row['canonical']}")
    for domain in domains_of(kb):
        data = load_domain(kb, domain)
        for kind, field in (("entities", "译名"), ("glossary", "译法")):
            for section in data[kind]:
                translation = _known_translation(section["fields"].get(field))
                if translation:
                    yield (domain, _identity_names(section["name"]), translation,
                           f"{domain}/{kind}.md {section['name']}")


# ---------------------------------------------------------------- init / status / check

def init(home_arg=None):
    home = Path(home_arg).expanduser().resolve() if home_arg else home_dir()
    cfg_path = home / "config.json"
    if not cfg_path.exists():
        _write(cfg_path, json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n")
        print(f"OK: config -> {cfg_path}")
    else:
        print(f"skip: config exists -> {cfg_path}")
    kb = kb_path(home)
    if (kb / "aliases.tsv").exists():
        print(f"skip: knowledge base exists -> {kb}")
    else:
        _write(kb / "aliases.tsv", "\t".join(ALIAS_COLUMNS) + "\n")
        _write(kb / "index.md", INDEX_TEMPLATE)
        _write(kb / ".gitignore", "*.bak\n")
        init_domain(kb, "seiyuu")
        print(f"OK: knowledge base -> {kb}")
    if not (kb / ".git").exists():
        r = _git(kb, "init", "-q")
        if r is None:
            print("WARN: git not found; the knowledge base is not version-controlled")
        elif r.returncode == 0:
            _git(kb, "add", "-A")
            _git(kb, "-c", "user.name=kb_tools", "-c", "user.email=kb@local", "commit", "-q", "-m", "chore: init knowledge base")
            print(f"OK: git init -> {kb}")
    if home_arg and home != home_dir():
        print(f"NOTE: set TRANSLATE_SRT_HOME={home} so other sessions find it")
    return 0


def init_domain(kb, domain):
    d = kb / domain
    _write(d / "entities.md", ENTITIES_TEMPLATE.format(domain=domain))
    _write(d / "glossary.md", GLOSSARY_TEMPLATE.format(domain=domain))
    _write(d / "style.md", STYLE_TEMPLATE.format(domain=domain))
    _write(d / "sources.md", SOURCES_TEMPLATE.format(domain=domain))


def status():
    home = home_dir()
    cfg = load_config(home)
    kb = kb_path(home, cfg)
    print(f"home:      {home}")
    print(f"config:    {home / 'config.json'} {'(exists)' if (home / 'config.json').is_file() else '(missing, using defaults)'}")
    print(f"knowledge: {kb} {'(exists)' if (kb / 'aliases.tsv').is_file() else '(MISSING - run init)'}")
    print(f"settings:  target={cfg['default_target_lang']} review_mode={cfg['review_mode']} volatile_recheck_months={cfg['volatile_recheck_months']}")
    if not (kb / "aliases.tsv").is_file():
        return 1
    rows = load_aliases(kb)
    by_domain = Counter(r["domain"] for r in rows)
    auto = sum(1 for r in rows if r["mode"] == "auto")
    print(f"aliases:   {len(rows)} rows ({auto} auto), {sum(len(r['variants']) for r in rows)} asr variants")
    for dom in domains_of(kb):
        d = load_domain(kb, dom)
        print(f"  {dom}: aliases={by_domain.get(dom, 0)} entities={len(d['entities'])} glossary={len(d['glossary'])} "
              f"sources={sum(1 for l in d['sources'].splitlines() if l.startswith('- '))}")
    r = _git(kb, "status", "--short")
    if r is not None and r.returncode == 0:
        dirty = [l for l in r.stdout.splitlines() if l.strip()]
        print(f"git:       {'clean' if not dirty else f'{len(dirty)} uncommitted change(s)'}")
    return 0


def check(kb=None, quiet=False):
    kb = kb or require_kb()
    rows = load_aliases(kb)
    problems, warns = [], []
    seen = {}
    variant_owner = {}
    for r in rows:
        where = f"aliases.tsv:{r['_line']} {r['canonical']}"
        if r["type"] not in ALIAS_TYPES:
            problems.append(f"{where}: type must be one of {sorted(ALIAS_TYPES)}, got {r['type']!r}")
        if r["mode"] not in ALIAS_MODES:
            problems.append(f"{where}: mode must be auto|ask, got {r['mode']!r}")
        if not r["domain"]:
            problems.append(f"{where}: domain is empty")
        elif not (kb / r["domain"] / "entities.md").exists():
            problems.append(f"{where}: domain dir {r['domain']}/ does not exist")
        if r["canonical"] in seen:
            problems.append(f"{where}: duplicate canonical (also line {seen[r['canonical']]})")
        seen[r["canonical"]] = r["_line"]
        for v in r["variants"]:
            if v == r["canonical"]:
                warns.append(f"{where}: variant equals canonical: {v}")
            elif v in r["canonical"]:
                problems.append(f"{where}: variant {v!r} is a substring of its canonical -- replace would loop; drop it")
            if v in variant_owner and variant_owner[v] != r["canonical"]:
                problems.append(f"{where}: variant {v!r} also listed under {variant_owner[v]!r}")
            variant_owner[v] = r["canonical"]
            if r["mode"] == "auto" and len(v) < AUTO_MIN_LEN:
                warns.append(f"{where}: auto variant {v!r} is shorter than {AUTO_MIN_LEN} chars -- likely to hit inside other words; consider mode=ask")
    # auto 变体是别的 canonical 的子串:替换会把那个正确的名字改坏
    canon_all = [r["canonical"] for r in rows]
    for r in rows:
        if r["mode"] != "auto":
            continue
        for v in r["variants"]:
            for c in canon_all:
                if c != r["canonical"] and v in c:
                    problems.append(f"aliases.tsv:{r['_line']} {r['canonical']}: auto variant {v!r} is inside another canonical {c!r}")
    for dom in domains_of(kb):
        d = load_domain(kb, dom)
        for kind in ("entities", "glossary"):
            names = Counter(s["name"] for s in d[kind])
            for n, c in names.items():
                if c > 1:
                    problems.append(f"{dom}/{kind}.md: duplicate section {n!r}")
            for s in d[kind]:
                st = s["fields"].get("稳定性", "")
                if st and not STABILITY_RE.match(st):
                    problems.append(f"{dom}/{kind}.md {s['name']}: 稳定性 must be stable or volatile(YYYY-MM), got {st!r}")
                short = [n for n in section_names(s) if len(n) < MATCH_MIN_LEN]
                if short:
                    warns.append(f"{dom}/{kind}.md {s['name']}: single-char surface {short} is ignored by match")
                if kind == "entities" and "译名" not in s["fields"]:
                    warns.append(f"{dom}/entities.md {s['name']}: no 译名 field")
                if kind == "glossary" and "译法" not in s["fields"]:
                    warns.append(f"{dom}/glossary.md {s['name']}: no 译法 field")
    translations = {}
    for domain, names, translation, where in _translation_records(kb):
        for name in sorted(names):
            previous = translations.get((domain, name))
            if previous and previous[0] != translation:
                problems.append(f"{where}: translation {translation!r} conflicts with "
                                f"{previous[0]!r} in {previous[1]} [{domain}/{name}]")
            else:
                translations[(domain, name)] = (translation, where)
    if not quiet:
        for p in problems:
            print(f"ERROR {p}")
        for w in warns:
            print(f"WARN  {w}")
        print(f"{'FAIL' if problems else 'OK'}: {len(rows)} alias rows, {len(domains_of(kb))} domains, "
              f"{len(problems)} error(s), {len(warns)} warning(s)")
    return 1 if problems else 0


# ---------------------------------------------------------------- match / replace

def build_patterns(kb, domains=None):
    """知识库 -> 可匹配写法表。返回 list of {surface, canonical, domain, kind, row/section, mode, is_variant}。"""
    pats = []
    for r in load_aliases(kb):
        if domains and r["domain"] not in domains:
            continue
        pats.append({"surface": r["canonical"], "canonical": r["canonical"], "domain": r["domain"],
                     "kind": "alias", "row": r, "mode": r["mode"], "is_variant": False})
        for v in r["variants"]:
            pats.append({"surface": v, "canonical": r["canonical"], "domain": r["domain"],
                         "kind": "alias", "row": r, "mode": r["mode"], "is_variant": True})
    covered = {p["surface"] for p in pats}
    for dom in domains or domains_of(kb):
        d = load_domain(kb, dom)
        for kind, secs in (("entity", d["entities"]), ("term", d["glossary"])):
            for s in secs:
                for n in section_names(s):
                    # 单字写法(如「生放送 / 生」里的「生」)到处撞,不参与匹配
                    if n in covered or len(n) < MATCH_MIN_LEN:
                        continue
                    covered.add(n)
                    pats.append({"surface": n, "canonical": s["name"], "domain": dom, "kind": kind,
                                 "section": s, "mode": "ask", "is_variant": False})
    return pats


def matching_spans(text, pats):
    """Claim longest surfaces in the original text; correct forms win equal-length ties."""
    claimed, spans = [False] * len(text), []
    for pattern in sorted(pats, key=lambda p: (-len(p["surface"]), p["is_variant"])):
        surface = pattern["surface"]
        if not surface:
            continue
        start = 0
        while True:
            pos = text.find(surface, start)
            if pos < 0:
                break
            end = pos + len(surface)
            if not any(claimed[pos:end]):
                claimed[pos:end] = [True] * len(surface)
                spans.append((pos, end, pattern))
            start = pos + 1
    return sorted(spans, key=lambda span: span[0])


def match_entries(entries, pats):
    """返回 {canonical: {..., count, entries[], matched{surface: count}}},按 count 降序。"""
    hits = {}
    # 同一 canonical 既有别名行又有档案时,命中的元数据以别名行为准(译名、类型、mode 都在那里)
    meta = {}
    for p in pats:
        if p["kind"] == "alias" or p["canonical"] not in meta:
            meta[p["canonical"]] = p
    for i, e in enumerate(entries, 1):
        for _, _, p in matching_spans(e["text"], pats):
            m = meta[p["canonical"]]
            h = hits.setdefault(p["canonical"], {
                "canonical": p["canonical"], "domain": m["domain"], "kind": m["kind"], "mode": m["mode"],
                "translation": m["row"]["translation"] if m["kind"] == "alias" else
                (m["section"]["fields"].get("译名") or m["section"]["fields"].get("译法", "")),
                "type": m["row"]["type"] if m["kind"] == "alias" else m["kind"],
                "count": 0, "entries": [], "matched": {}, "variant_entries": [],
            })
            h["count"] += 1
            if i not in h["entries"]:
                h["entries"].append(i)
            h["matched"][p["surface"]] = h["matched"].get(p["surface"], 0) + 1
            if p["is_variant"] and i not in h["variant_entries"]:
                h["variant_entries"].append(i)
    return dict(sorted(hits.items(), key=lambda kv: -kv[1]["count"]))


def load_stopwords(kb):
    words = set(KATAKANA_STOPWORDS)
    extra = _read(kb / "stopwords.txt")
    words.update(w.strip() for w in extra.splitlines() if w.strip() and not w.startswith("#"))
    return words


def candidates(entries, pats, stopwords=frozenset(), limit=40):
    """未被知识库覆盖的片假名串 / 拉丁词,按出现次数排序 -> 缺口候选。"""
    surfaces = [p["surface"] for p in pats]
    cnt, where = Counter(), defaultdict(list)
    for i, e in enumerate(entries, 1):
        _, body = _split_speaker(e["text"])
        for m in list(KATAKANA_RUN.finditer(body)) + list(LATIN_RUN.finditer(body)):
            w = m.group(0)
            if w in stopwords or any(w in s or s in w for s in surfaces):
                continue
            cnt[w] += 1
            if i not in where[w] and len(where[w]) < 5:
                where[w].append(i)
    return [{"word": w, "count": c, "entries": where[w]} for w, c in cnt.most_common(limit)]


def match(srt_path, out=None, domains=None, show_candidates=True):
    kb = require_kb()
    entries = parse(Path(srt_path).read_text(encoding="utf-8-sig"))
    pats = build_patterns(kb, domains)
    hits = match_entries(entries, pats)
    dom_names, dom_hits = Counter(), Counter()
    for h in hits.values():
        dom_names[h["domain"]] += 1
        dom_hits[h["domain"]] += h["count"]
    cands = candidates(entries, pats, load_stopwords(kb)) if show_candidates else []
    result = {
        "srt": str(Path(srt_path).resolve()),
        "entries": len(entries),
        "domains": [{"domain": d, "names": dom_names[d], "hits": dom_hits[d]}
                    for d in sorted(dom_names, key=lambda d: -dom_hits[d])],
        "hits": list(hits.values()),
        "candidates": cands,
    }
    print(f"match: {len(entries)} entries x {len(pats)} surfaces -> {len(hits)} names hit, {sum(dom_hits.values())} occurrences")
    if not result["domains"]:
        print("  domains: (none) -- nothing in the knowledge base matched; treat as a new domain or skip loading")
    for d in result["domains"]:
        print(f"  domain {d['domain']}: {d['names']} names / {d['hits']} occurrences")
    for h in list(hits.values())[:25]:
        via = ", ".join(f"{s}x{c}" for s, c in sorted(h["matched"].items(), key=lambda kv: -kv[1]))
        asr = f"  ASR-variant in {len(h['variant_entries'])} entries" if h["variant_entries"] else ""
        print(f"  {h['canonical']} -> {h['translation'] or '(no translation)'}  [{h['domain']}/{h['type']}/{h['mode']}] x{h['count']} via {via}{asr}")
    if len(hits) > 25:
        print(f"  ... {len(hits) - 25} more in hits.json")
    if cands:
        print(f"  candidates not in the knowledge base (check these when reading through):")
        for c in cands[:20]:
            print(f"    {c['word']} x{c['count']}  #{', #'.join(map(str, c['entries']))}")
    if out:
        _write(Path(out), json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"OK: hits -> {out}")
    return 0


def replace(srt_path, out=None, domains=None, log=None):
    kb = require_kb()
    if check(kb, quiet=True):
        sys.exit("ERROR: knowledge base fails `check`; fix it before replacing subtitles")
    pats = build_patterns(kb, domains)
    text = Path(srt_path).read_text(encoding="utf-8-sig")
    entries = parse(text)
    if not entries:
        sys.exit("ERROR: no subtitle entries parsed")
    changes, asks, records = [], [], []
    for i, e in enumerate(entries, 1):
        original = e["text"]
        pieces, cursor = [], 0
        for start, end, pattern in matching_spans(original, pats):
            if pattern["kind"] != "alias" or not pattern["is_variant"]:
                continue
            variant, canonical, mode = pattern["surface"], pattern["canonical"], pattern["mode"]
            records.append({"entry": i, "variant": variant, "canonical": canonical, "status": mode,
                            "start_ms": e["start"], "end_ms": e["end"], "source_text": original,
                            "current_entries": str(i)})
            if mode == "auto":
                changes.append((i, variant, canonical))
                pieces.extend((original[cursor:start], canonical))
                cursor = end
            else:
                asks.append((i, variant, canonical))
        pieces.append(original[cursor:])
        e["text"] = "".join(pieces)
    dest = out or srt_path
    _write(Path(dest), serialize(entries))
    print(f"OK: {len(entries)} entries -> {dest}")
    per = Counter((v, c) for _, v, c in changes)
    print(f"  replaced (mode=auto): {len(changes)} occurrence(s), {len(per)} distinct")
    for (v, c), n in per.most_common():
        ents = [str(i) for i in sorted({i for i, vv, _ in changes if vv == v})][:8]
        print(f"    {v} -> {c}  x{n}  #{', #'.join(ents)}{' ...' if n > 8 else ''}")
    if asks:
        per_ask = Counter((v, c) for _, v, c in asks)
        print(f"  NOT replaced (mode=ask), fix by hand in 3b if they really are mishearings:")
        for (v, c), n in per_ask.most_common():
            ents = [str(i) for i in sorted({i for i, vv, _ in asks if vv == v})][:8]
            print(f"    {v} -> {c}?  x{n}  #{', #'.join(ents)}{' ...' if n > 8 else ''}")
    if log:
        _write_alias_log(Path(log), records)
        print(f"OK: log -> {log}")
    return 0


def _write_alias_log(path, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ALIAS_LOG_COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write(path, buf.getvalue())


def remap_log(srt_path, log, out=None):
    """Map immutable time spans to current entry numbers after merge/split/deletion."""
    entries = parse(Path(srt_path).read_text(encoding="utf-8-sig"))
    if not entries:
        print("ERROR: no subtitle entries parsed; log was not changed")
        return 2
    try:
        with open(log, encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if reader.fieldnames != ALIAS_LOG_COLUMNS:
                raise ValueError("log must include timestamp anchors from the current replace command")
            rows = list(reader)
        unmatched = []
        for number, row in enumerate(rows, 1):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"malformed log row {number}")
            start, end = int(row["start_ms"]), int(row["end_ms"])
            if end <= start:
                raise ValueError(f"invalid time span in log row {number}")
            current = [str(i) for i, entry in enumerate(entries, 1)
                       if entry["start"] < end and entry["end"] > start]
            row["current_entries"] = ";".join(current)
            if not current:
                unmatched.append(number)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}; log was not changed")
        return 2
    destination = out or log
    _write_alias_log(Path(destination), rows)
    print(f"OK: mapped {len(rows)} log records against {srt_path} -> {destination}")
    if unmatched:
        print(f"WARN: no current entry overlaps log rows {unmatched}; inspect source_text for deleted/moved content")
    return 1 if unmatched else 0


# ---------------------------------------------------------------- glossary

def build_glossary(srt_path, out, domains=None, video_date=None, stem=None):
    kb = require_kb()
    if check(kb, quiet=True):
        sys.exit("ERROR: knowledge base fails `check`; fix it before building a glossary")
    cfg = load_config()
    months = int(cfg.get("volatile_recheck_months", 3))
    vdate = dt.date.fromisoformat(video_date) if video_date else dt.date.today()
    entries = parse(Path(srt_path).read_text(encoding="utf-8-sig"))
    if not domains:
        hits_all = match_entries(entries, build_patterns(kb))
        domains = sorted({h["domain"] for h in hits_all.values()})
    domains = list(dict.fromkeys(domains))
    # Match each selected domain independently so homographs cannot consume another domain's hits.
    domain_hits = {dom: match_entries(entries, build_patterns(kb, [dom])) for dom in domains}
    hits = [hit for selected in domain_hits.values() for hit in selected.values()]
    stem = stem or Path(srt_path).stem
    alias_rows = {r["canonical"]: r for r in load_aliases(kb)}
    md = [f"# 术语表 — {stem}", "",
          f"> 「来自知识库」一节由 `kb_tools.py glossary` 生成于 {dt.date.today().isoformat()},"
          f"加载领域:{', '.join(domains) or '(无)'};视频日期按 {vdate.isoformat()} 判定 volatile。",
          "> 「本次新增」一节由主代理在第 2d 步填写:本次调研新得的译名、自拟译名、听众昵称等。",
          "> 术语表须覆盖字幕中出现的每个专名(说话人名也在其中),查不到标「自拟」。", ""]
    md += ["## 来自知识库", ""]
    alias_hits = [h for h in hits if h["kind"] == "alias"]
    md += ["### 专名", ""]
    if alias_hits:
        md += ["| 原文 | 译名 | 类型 | 出现 | ASR 常见错听 | 备注 |", "| --- | --- | --- | --- | --- | --- |"]
        for h in alias_hits:
            r = alias_rows[h["canonical"]]
            md.append(f"| {r['canonical']} | {r['translation'] or '(未定)'} | {r['type']} | {h['count']} | "
                      f"{'; '.join(r['variants'])} | {r['notes']} |")
    else:
        md.append("(无命中)")
    md.append("")
    stale = []
    for dom in domains:
        d = load_domain(kb, dom)
        for kind, title in (("entities", "人物/作品档案"), ("glossary", "术语")):
            secs = [s for s in d[kind] if any(n in domain_hits[dom] for n in [s["name"]] + section_names(s))]
            if not secs:
                continue
            md += [f"### {title}({dom})", ""]
            for s in secs:
                st = stability_state(s["fields"].get("稳定性", ""), vdate, months)
                if st == "stale":
                    stale.append((dom, kind, s))
                    md.append(f"> ⚠ volatile 已过期({s['fields'].get('稳定性')}),下面的内容可能过时,列入缺口清单重查。")
                    md.append("")
                md.append(render_section(s, level=4).rstrip() + "\n")
    if stale:
        md += ["### 需重查(volatile 已过期)", ""]
        md += [f"- {s['name']}({dom}/{kind},{s['fields'].get('稳定性')})" for dom, kind, s in stale]
        md.append("")
    md += ["## 本次新增", "", "(待填)", ""]
    _write(Path(out), "\n".join(md))
    n_ent = sum(1 for _ in re.finditer(r"^### ", "\n".join(md), re.M))
    print(f"OK: glossary -> {out}  (domains: {', '.join(domains) or '-'}; {len(alias_hits)} aliases, "
          f"{len(hits) - len(alias_hits)} entity/term sections, {len(stale)} stale)")
    return 0


# ---------------------------------------------------------------- backfill

def backfill(dirs, out):
    projects = []
    for d in dirs:
        root = Path(d).expanduser().resolve()
        for g in sorted(root.rglob("_context/glossary.md")):
            projects.append(g.parent.parent)
    if not projects:
        sys.exit(f"ERROR: no <project>/_context/glossary.md found under {dirs}")
    md = ["# 沉淀素材(backfill staging)", "",
          f"> 由 `kb_tools.py backfill` 于 {dt.date.today().isoformat()} 汇总自 {len(projects)} 个历史项目。",
          "> 沉淀子代理按 sediment_rules.md 处理本文件,产出 proposal.json;不要直接把这里的内容抄进知识库。", ""]
    for p in projects:
        ctx = p / "_context"
        mtime = dt.date.fromtimestamp(ctx.stat().st_mtime).isoformat()
        md += [f"---", "", f"# 项目 {p.name}", "", f"- 路径:{p}", f"- 最后修改:{mtime}", ""]
        for name in ("glossary.md", "brief.md", "review_notes.md"):
            f = ctx / name
            if f.is_file():
                md += [f"## {p.name}/{name}", "", _read(f).strip(), ""]
        research = sorted((ctx / "research").glob("*.md")) if (ctx / "research").is_dir() else []
        if research:
            md += [f"## {p.name}/research/", ""]
            for f in research:
                md += [f"### {f.name}", "", _read(f).strip(), ""]
    _write(Path(out), "\n".join(md) + "\n")
    print(f"OK: {len(projects)} project(s) -> {out}")
    for p in projects:
        print(f"  - {p}")
    return 0


# ---------------------------------------------------------------- apply proposal

def _norm_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x for x in SEP_RE.split(v) if x]
    return [str(x).strip() for x in v if str(x).strip()]


def _validate_proposal_paths(kb, prop):
    """A domain is one directory name, never a path out of the candidate tree."""
    if not isinstance(prop, dict):
        raise ValueError("proposal must be a JSON object")
    for kind in ("index", "aliases", "entities", "glossary", "sources", "style"):
        items = prop.get(kind, [])
        if not isinstance(items, list) or any(not isinstance(it, dict) for it in items):
            raise ValueError(f"{kind} must be an array of objects")
        for it in items:
            domain = it.get("domain")
            if domain is None and kind == "aliases":
                continue  # Existing aliases may be updated by canonical alone.
            if (not isinstance(domain, str) or not domain or domain.startswith(".")
                    or domain != domain.strip() or any(c in domain for c in '/\\:<>|?*"\r\n\0')
                    or not (kb / domain).resolve().is_relative_to(kb.resolve())):
                raise ValueError(f"invalid domain directory: {domain!r}")


def _snapshot_files(root):
    """Capture the working files, excluding Git metadata and refusing linked paths."""
    snapshot = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in dirs + files:
            path = Path(directory) / name
            if name != ".git" and (path.is_symlink()
                                  or not path.resolve().is_relative_to(root.resolve())):
                raise ValueError(f"knowledge base contains a linked path: {path}")
        for name in files:
            if name != ".git":
                path = Path(directory) / name
                snapshot[path.relative_to(root)] = path.read_bytes()
    return snapshot


def _replace_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kb-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _publish_snapshot(kb, before, after):
    if _snapshot_files(kb) != before:
        raise RuntimeError("knowledge base changed during apply; retry against the current files")
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    written, new_dirs = [], set()
    try:
        for relative in changed:
            path = kb / relative
            for parent in path.parents:
                if parent == kb:
                    break
                if not parent.exists():
                    new_dirs.add(parent)
            if relative in after:
                _replace_bytes(path, after[relative])
            else:
                path.unlink()
            written.append(relative)
    except OSError:
        for relative in reversed(written):
            if relative in before:
                _replace_bytes(kb / relative, before[relative])
            else:
                (kb / relative).unlink(missing_ok=True)
        for directory in sorted(new_dirs, key=lambda p: len(p.parts), reverse=True):
            if directory.exists():
                directory.rmdir()
        raise


def apply_proposal(proposal_path, commit=False, summary_out=None):
    kb = require_kb()
    if check(kb, quiet=True):
        sys.exit("ERROR: knowledge base fails `check`; fix it before applying a proposal")
    try:
        prop = json.loads(Path(proposal_path).read_text(encoding="utf-8-sig"))
        _validate_proposal_paths(kb, prop)
        project = prop.get("project", Path(proposal_path).parent.parent.name)
        date = prop.get("date", dt.date.today().isoformat())
        source_tag = f"{project} ({date})"
        before = _snapshot_files(kb)
        with tempfile.TemporaryDirectory(prefix=".translate-srt-apply-", dir=kb.parent) as temporary:
            candidate = Path(temporary) / "knowledge"
            candidate.mkdir()
            for relative, data in before.items():
                path = candidate / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            added, updated, conflicts, skipped = _merge_proposal(candidate, prop, source_tag)
            if check(candidate, quiet=True):
                check(candidate)
                message = "ERROR: proposal fails validation; knowledge base and Git index were not changed"
                print(message)
                if summary_out:
                    _write(Path(summary_out), f"# 沉淀失败 — {source_tag}\n\n{message}\n")
                return 2
            _publish_snapshot(kb, before, _snapshot_files(candidate))
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"ERROR: apply failed: {exc}")
        return 2

    lines = [f"# 沉淀结果 — {source_tag}", "",
             f"新增 {len(added)} · 更新 {len(updated)} · 冲突 {len(conflicts)} · 跳过 {len(skipped)}", ""]
    for title, items in (("## CONFLICT(未写入,需要你裁决)", conflicts), ("## 新增", added),
                         ("## 更新", updated), ("## 跳过", skipped)):
        if items:
            lines += [title, ""] + [f"- {x}" for x in items] + [""]
    summary = "\n".join(lines)
    print(summary)
    if summary_out:
        _write(Path(summary_out), summary + "\n")
        print(f"OK: summary -> {summary_out}")
    if commit:
        state = _git(kb, "status", "--porcelain", check=True)
        if state is None:
            print("ERROR: git not found; validated changes remain uncommitted")
            return 2
        if state.stdout:
            _git(kb, "add", "-A", check=True)
            result = _git(kb, "commit", "-q", "-m", f"kb: sediment from {source_tag}")
            if result is None or result.returncode:
                print(f"ERROR: commit failed; validated changes remain uncommitted:\n{result.stderr if result else ''}")
                return 2
            print("OK: committed")
        else:
            print("OK: no changes to commit")
    else:
        print(f"next: review `kb_tools.py diff`, then commit the knowledge base at {kb}")
    return 1 if conflicts else 0


def _merge_proposal(kb, prop, source_tag):
    """Merge only into a disposable candidate; the caller validates before publishing."""
    added, updated, conflicts, skipped = [], [], [], []
    translations = {}
    for domain, names, translation, where in _translation_records(kb):
        for name in names:
            translations[(domain, name)] = (translation, where)

    def resolve_translation(domain, name, value, where, title=True):
        proposed = _known_translation(value)
        names = _identity_names(name) if title else {name}
        known = {translations[(domain, n)] for n in names if (domain, n) in translations}
        values = {translation for translation, _ in known}
        if len(values) > 1 or (proposed and values and proposed not in values):
            details = "; ".join(f"{translation!r} in {origin}" for translation, origin in sorted(known))
            conflicts.append(f"{where}: KB translation {details} vs proposal {proposed!r} "
                             f"({source_tag}) -- kept KB; conflicting translation not written")
            return False, ""
        accepted = proposed or next(iter(values), "")
        if accepted:
            for n in names:
                translations[(domain, n)] = (accepted, where)
        return True, accepted

    # 新领域
    for dom in prop.get("index", []):
        name = dom["domain"]
        if not (kb / name / "entities.md").exists():
            init_domain(kb, name)
            block = [f"## {name}", dom.get("description", "").strip()]
            if dom.get("keywords"):
                block.append("关键词:" + " / ".join(_norm_list(dom["keywords"])))
            if dom.get("regulars"):
                block.append("常驻人物:" + " / ".join(_norm_list(dom["regulars"])))
            _write(kb / "index.md", _read(kb / "index.md").rstrip() + "\n\n" + "\n".join(block) + "\n")
            added.append(f"domain {name}")
        else:
            skipped.append(f"domain {name}: already exists (edit index.md by hand)")

    # aliases
    rows = load_aliases(kb)
    by_canon = {r["canonical"]: r for r in rows}
    variant_owner = {v: r["canonical"] for r in rows for v in r["variants"]}
    for a in prop.get("aliases", []):
        canon = a["canonical"].strip()
        variants = [v for v in _norm_list(a.get("asr_variants")) if v and v != canon and v not in canon]
        if canon in by_canon:
            r = by_canon[canon]
            accepted, translation = resolve_translation(r["domain"], canon, a.get("translation"),
                                                        f"alias {canon}", title=False)
            if accepted and translation and not _known_translation(r["translation"]):
                r["translation"] = translation
                updated.append(f"alias {canon}: translation set to {r['translation']!r}")
            new_v = [v for v in variants if v not in r["variants"] and variant_owner.get(v, canon) == canon]
            for v in variants:
                if variant_owner.get(v, canon) != canon:
                    conflicts.append(f"alias {canon}: variant {v!r} already belongs to {variant_owner[v]!r} -- not added")
            if new_v:
                r["variants"].extend(new_v)
                for v in new_v:
                    variant_owner[v] = canon
                updated.append(f"alias {canon}: +variants {new_v}")
            if r["mode"] == "auto" and (a.get("mode") == "ask" or (new_v and a.get("mode") != "auto")):
                r["mode"] = "ask"
                updated.append(f"alias {canon}: mode auto -> ask (variants require confirmation)")
            if a.get("mode") == "auto" and r["mode"] == "ask":
                r["mode"] = "auto"
                updated.append(f"alias {canon}: mode ask -> auto")
            if a.get("notes") and a["notes"].strip() not in r["notes"]:
                r["notes"] = (r["notes"] + "; " if r["notes"] else "") + a["notes"].strip()
        else:
            bad = [v for v in variants if v in variant_owner]
            for v in bad:
                conflicts.append(f"alias {canon}: variant {v!r} already belongs to {variant_owner[v]!r} -- not added")
            variants = [v for v in variants if v not in bad]
            r = {"canonical": canon, "variants": variants, "translation": (a.get("translation") or "").strip(),
                 "domain": a.get("domain", "").strip(), "type": a.get("type", "term").strip(),
                 "mode": a.get("mode", "ask").strip() or "ask", "notes": (a.get("notes") or "").strip(), "_line": 0}
            if r["type"] not in ALIAS_TYPES:
                skipped.append(f"alias {canon}: bad type {r['type']!r}")
                continue
            if not (kb / r["domain"] / "entities.md").exists():
                skipped.append(f"alias {canon}: unknown domain {r['domain']!r}")
                continue
            accepted, translation = resolve_translation(r["domain"], canon, r["translation"],
                                                        f"alias {canon}", title=False)
            if not accepted:
                continue
            r["translation"] = translation
            rows.append(r)
            by_canon[canon] = r
            for v in variants:
                variant_owner[v] = canon
            added.append(f"alias {canon} -> {r['translation'] or '?'} [{r['domain']}/{r['type']}/{r['mode']}] variants={variants}")
    save_aliases(kb, rows)

    # entities / glossary sections
    def merge_sections(kind, items, key_field, name_key):
        by_dom = defaultdict(list)
        for it in items:
            by_dom[it["domain"]].append(it)
        for dom, its in by_dom.items():
            path = kb / dom / f"{kind}.md"
            if not path.exists():
                skipped.extend(f"{kind} {it[name_key]}: unknown domain {dom!r}" for it in its)
                continue
            text = _read(path)
            secs = parse_sections(text)
            by_name = {s["name"]: s for s in secs}
            for s in secs:
                for n in section_names(s):
                    by_name.setdefault(n, s)
            head = text.split("\n### ", 1)[0].rstrip() + "\n\n" if "### " in text else text.rstrip() + "\n\n"
            for it in its:
                name = it[name_key].strip()
                if name in by_name:
                    s = by_name[name]
                    accepted, tr = resolve_translation(dom, s["name"], it.get("translation"), f"{kind} {name}")
                    if accepted and tr and not _known_translation(s["fields"].get(key_field)):
                        _set_field(s, key_field, tr)
                    extra = []
                    for al in _norm_list(it.get("aliases")):
                        if al not in section_names(s) and al != name:
                            cur = s["fields"].get("别名", "")
                            s["fields"]["别名"] = (cur + "; " if cur else "") + al
                            extra.append(al)
                    if extra:
                        _set_field(s, "别名", s["fields"]["别名"])
                    if it.get("summary") and it["summary"].strip() not in "\n".join(s["lines"]):
                        s["lines"].append(f"- 补充({source_tag}): {it['summary'].strip()}")
                    if it.get("notes") and it["notes"].strip() not in "\n".join(s["lines"]):
                        s["lines"].append(f"- 补充({source_tag}): {it['notes'].strip()}")
                    if it.get("stability") and it["stability"] != s["fields"].get("稳定性"):
                        if STABILITY_RE.match(it["stability"]):
                            _set_field(s, "稳定性", it["stability"])
                    updated.append(f"{kind} {name}" + (f": +别名 {extra}" if extra else ": appended"))
                else:
                    accepted, tr = resolve_translation(dom, name, it.get("translation"), f"{kind} {name}")
                    if not accepted:
                        continue
                    lines = []
                    if kind == "entities":
                        lines.append(f"- 类型: {it.get('type', 'person')}")
                        lines.append(f"- 译名: {tr or '(未定)'}")
                        if _norm_list(it.get("aliases")):
                            lines.append(f"- 别名: {'; '.join(_norm_list(it.get('aliases')))}")
                        if it.get("summary"):
                            lines.append(f"- 简介: {it['summary'].strip()}")
                    else:
                        lines.append(f"- 译法: {tr or '(未定)'}")
                        if it.get("notes"):
                            lines.append(f"- 备注: {it['notes'].strip()}")
                    st = it.get("stability", "stable")
                    lines.append(f"- 稳定性: {st if STABILITY_RE.match(st or '') else 'stable'}")
                    lines.append(f"- 来源: {source_tag}")
                    s = parse_sections(f"### {name}\n" + "\n".join(lines))[0]
                    secs.append(s)
                    by_name[name] = s
                    added.append(f"{kind} {name} -> {tr or '?'} [{dom}]")
            _write(path, head + "".join(render_section(s) for s in secs))

    merge_sections("entities", prop.get("entities", []), "译名", "name")
    merge_sections("glossary", prop.get("glossary", []), "译法", "term")

    # sources
    by_dom = defaultdict(list)
    for s in prop.get("sources", []):
        by_dom[s["domain"]].append(s)
    for dom, its in by_dom.items():
        path = kb / dom / "sources.md"
        if not path.exists():
            skipped.extend(f"source {it['url']}: unknown domain {dom!r}" for it in its)
            continue
        text = _read(path)
        new = [f"- {it['url'].strip()} — {it.get('note', '').strip()}" for it in its if it["url"].strip() not in text]
        if new:
            _write(path, text.rstrip() + "\n" + "\n".join(new) + "\n")
            added.extend(f"source {it['url']} [{dom}]" for it in its if it["url"].strip() not in text)

    # style
    for st in prop.get("style", []):
        path = kb / st["domain"] / "style.md"
        if not path.exists():
            skipped.append(f"style: unknown domain {st['domain']!r}")
            continue
        text = _read(path)
        body = st["text"].strip()
        if body in text:
            skipped.append(f"style [{st['domain']}]: already present")
            continue
        section = st.get("section", "其他").strip()
        heading = f"## {section}"
        entry = f"- {body}(来源:{source_tag})"
        if heading in text:
            head, tail = text.split(heading, 1)
            nxt = re.search(r"\n## ", tail)
            if nxt:
                tail = tail[:nxt.start()].rstrip() + "\n" + entry + "\n" + tail[nxt.start():]
            else:
                tail = tail.rstrip() + "\n" + entry + "\n"
            text = head + heading + tail
        else:
            text = text.rstrip() + f"\n\n{heading}\n{entry}\n"
        _write(path, text)
        added.append(f"style [{st['domain']}/{section}]: {body[:40]}")

    return added, updated, conflicts, skipped


def _set_field(sec, key, value):
    for i, line in enumerate(sec["lines"]):
        if re.match(rf"^- +{re.escape(key)}\s*[:：]", line):
            sec["lines"][i] = f"- {key}: {value}"
            sec["fields"][key] = value
            return
    sec["lines"].append(f"- {key}: {value}")
    sec["fields"][key] = value


def diff():
    kb = require_kb()
    r = _git(kb, "status", "--short")
    if r is None or r.returncode != 0:
        sys.exit("ERROR: knowledge base is not a git repo")
    print(f"knowledge: {kb}")
    print(r.stdout or "clean\n")
    for label, options in (("staged", ("--cached",)), ("unstaged", ())):
        r = _git(kb, "diff", "--no-ext-diff", "--no-color", *options, check=True)
        if r.stdout:
            print(f"--- {label} ---")
            print(r.stdout)
    untracked = _git(kb, "ls-files", "--others", "--exclude-standard", "-z", check=True)
    for name in filter(None, untracked.stdout.split("\0")):
        path = kb / name
        print(f"--- untracked: {name} ---")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            print(f"Binary file: {name}")
            continue
        print("".join(difflib.unified_diff([], content.splitlines(keepends=True),
                                         fromfile="/dev/null", tofile=f"b/{name}")))
    return 0


# ---------------------------------------------------------------- self test

def self_test():
    with tempfile.TemporaryDirectory() as d:
        os.environ["TRANSLATE_SRT_HOME"] = d
        assert init(d) == 0
        kb = kb_path()
        assert (kb / "aliases.tsv").is_file() and (kb / "seiyuu" / "style.md").is_file()
        proposal = {
            "project": "t1", "date": "2026-09-05",
            "aliases": [
                {"canonical": "涼本あきほ", "asr_variants": ["鈴本あきほ", "涼本秋穂"], "translation": "凉本秋穗",
                 "domain": "seiyuu", "type": "person", "mode": "auto", "notes": "VIMS"},
                {"canonical": "永スタ", "asr_variants": "長スタ", "translation": "永Station", "domain": "seiyuu",
                 "type": "show", "mode": "ask"},
                {"canonical": "三角", "asr_variants": [], "translation": "三角桑", "domain": "seiyuu", "type": "nickname", "mode": "ask"},
            ],
            "entities": [{"domain": "seiyuu", "name": "涼本あきほ", "type": "person", "translation": "凉本秋穗",
                          "aliases": ["あきちゃん"], "summary": "VIMS 所属。", "stability": "stable"}],
            "glossary": [{"domain": "seiyuu", "term": "ふつおた", "translation": "普通来信", "notes": "普通邮件是错的"},
                         {"domain": "seiyuu", "term": "TIF", "translation": "东京偶像节", "stability": "volatile(2026-08)"}],
            "sources": [{"domain": "seiyuu", "url": "https://example.com/p", "note": "profile"}],
            "style": [{"domain": "seiyuu", "section": "敬称与称呼", "text": "ちゃん 译作 酱"}],
        }
        pp = Path(d) / "proposal.json"
        _write(pp, json.dumps(proposal, ensure_ascii=False))
        assert apply_proposal(str(pp)) == 0
        rows = load_aliases(kb)
        assert len(rows) == 3 and {r["canonical"] for r in rows} == {"涼本あきほ", "永スタ", "三角"}
        assert check(kb, quiet=True) == 0
        ent = load_domain(kb, "seiyuu")
        assert ent["entities"][0]["fields"]["别名"] == "あきちゃん"
        assert "ちゃん 译作 酱" in ent["style"]
        # 冲突:译名不同不覆盖;同名追加别名
        p2 = {"project": "t2", "aliases": [{"canonical": "涼本あきほ", "asr_variants": ["涼元あきほ"], "translation": "凉本明穗",
                                             "domain": "seiyuu", "type": "person"}],
              "entities": [{"domain": "seiyuu", "name": "涼本あきほ", "translation": "凉本秋穗", "aliases": ["あきほ"], "summary": "新补充"}]}
        _write(pp, json.dumps(p2, ensure_ascii=False))
        assert apply_proposal(str(pp)) == 1
        r = {r["canonical"]: r for r in load_aliases(kb)}["涼本あきほ"]
        assert r["translation"] == "凉本秋穗" and "涼元あきほ" in r["variants"]
        assert r["mode"] == "ask"  # A new unconfirmed variant downgrades the whole row.
        _write(pp, json.dumps({"project": "t3", "aliases": [
            {"canonical": "涼本あきほ", "domain": "seiyuu", "mode": "auto"}
        ]}, ensure_ascii=False))
        assert apply_proposal(str(pp)) == 0  # Explicit confirmation can promote it again.
        ent = load_domain(kb, "seiyuu")["entities"][0]
        assert "あきほ" in section_names(ent) and any("新补充" in l for l in ent["lines"])
        # match / replace / glossary
        srt = "1\n00:00:00,000 --> 00:00:01,000\n鈴本あきほ: 今日は鈴本あきほです\n\n2\n00:00:01,000 --> 00:00:02,000\n長スタとふつおた、あきちゃんとテナシーの話\n\n3\n00:00:02,000 --> 00:00:03,000\nTIF の話\n"
        sp = Path(d) / "t.srt"
        _write(sp, srt)
        hits_path = Path(d) / "hits.json"
        assert match(str(sp), str(hits_path)) == 0
        hits = json.loads(hits_path.read_text(encoding="utf-8"))
        names = {h["canonical"]: h for h in hits["hits"]}
        assert names["涼本あきほ"]["count"] == 3 and names["涼本あきほ"]["variant_entries"] == [1]
        assert "ふつおた" in names and "永スタ" in names and "TIF" in names
        assert any(c["word"] == "テナシー" for c in hits["candidates"])
        out = Path(d) / "out.srt"
        assert replace(str(sp), str(out)) == 0
        got = parse(out.read_text(encoding="utf-8"))
        assert got[0]["text"] == "涼本あきほ: 今日は涼本あきほです"
        assert "長スタ" in got[1]["text"]  # ask 不动
        gp = Path(d) / "glossary.md"
        assert build_glossary(str(sp), str(gp), video_date="2026-12-01") == 0
        g = gp.read_text(encoding="utf-8")
        assert "| 涼本あきほ | 凉本秋穗 |" in g and "### ふつおた" in g and "需重查" in g and "TIF" in g
        # backfill
        proj = Path(d) / "projs" / "x" / "_context"
        _write(proj / "glossary.md", "# g\n| a | b |\n")
        _write(proj / "research" / "01-a.md", "# r\n")
        st = Path(d) / "staging.md"
        assert backfill([str(Path(d) / "projs")], str(st)) == 0
        assert "01-a.md" in st.read_text(encoding="utf-8")
        # 稳定性判断
        assert stability_state("volatile(2026-06)", dt.date(2026, 9, 5), 3) == "stale"
        assert stability_state("volatile(2026-07)", dt.date(2026, 9, 5), 3) == "fresh"
        assert stability_state("stable", dt.date(2026, 9, 5), 3) == "stable"
    print("self-test OK")
    return 0


# ---------------------------------------------------------------- cli

def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["init", "status", "check", "match", "replace", "remap-log", "glossary",
                                                "backfill", "apply", "diff"])
    ap.add_argument("inputs", nargs="*", help="match/replace/glossary: in.srt;backfill: 目录;apply: proposal.json")
    ap.add_argument("-o", "--output")
    ap.add_argument("-d", "--domain", action="append", help="只用这些领域包(可重复);缺省全部/按命中自动选")
    ap.add_argument("--home", help="init: home 目录(缺省 $TRANSLATE_SRT_HOME 或 ~/.translate-srt)")
    ap.add_argument("--log", help="replace: 写带时间戳的 TSV;remap-log: 更新这份日志的 current_entries")
    ap.add_argument("--video-date", help="glossary: 视频日期 YYYY-MM-DD,判 volatile 条目是否过期(缺省今天)")
    ap.add_argument("--stem", help="glossary: 标题里用的项目名(缺省取 srt 词干)")
    ap.add_argument("--no-candidates", action="store_true", help="match: 不列未覆盖的片假名/拉丁词")
    ap.add_argument("--commit", action="store_true", help="apply: 合并后直接 git commit(review_mode=conflicts_only 且无冲突时用)")
    ap.add_argument("--summary", help="apply: 把结果摘要另存为 Markdown(如 _context/sediment_result.md)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.mode:
        ap.error("mode is required")
    if args.mode == "init":
        return sys.exit(init(args.home))
    if args.mode == "status":
        return sys.exit(status())
    if args.mode == "check":
        return sys.exit(check())
    if args.mode == "diff":
        return sys.exit(diff())
    if not args.inputs:
        ap.error(f"{args.mode} needs an input")
    if args.mode == "match":
        return sys.exit(match(args.inputs[0], args.output, args.domain, not args.no_candidates))
    if args.mode == "replace":
        return sys.exit(replace(args.inputs[0], args.output, args.domain, args.log))
    if args.mode == "remap-log":
        if not args.log:
            ap.error("remap-log needs --log <alias_log.tsv>")
        return sys.exit(remap_log(args.inputs[0], args.log, args.output))
    if args.mode == "glossary":
        if not args.output:
            ap.error("glossary needs -o <glossary.md>")
        return sys.exit(build_glossary(args.inputs[0], args.output, args.domain, args.video_date, args.stem))
    if args.mode == "backfill":
        if not args.output:
            ap.error("backfill needs -o <staging.md>")
        return sys.exit(backfill(args.inputs, args.output))
    if args.mode == "apply":
        return sys.exit(apply_proposal(args.inputs[0], args.commit, args.summary))


if __name__ == "__main__":
    main()
