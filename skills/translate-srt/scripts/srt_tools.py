#!/usr/bin/env python3
"""SRT 清理工具。文本规范的完整定义见 ../references/subtitle-rules.md。

用法:
  python srt_tools.py init <original.srt>      # 建工作区:复制原 srt + AGENTS.md + _context/(占位 brief/glossary/gaps + 空 research)
  python srt_tools.py stats in.srt                  # 体检条目形态,判定该 merge(被切碎)还是 split(多句粘连)
  python srt_tools.py speakers in.srt               # 列出说话人前缀(`名字: ` / `[S01] `)
  python srt_tools.py speakers in.srt --map S01=関根瞳  # 占位标签换真名,统一为 `名字: `
  python srt_tools.py speakers in.srt --drop        # 去掉说话人前缀(单说话人字幕)
  python srt_tools.py merge in.srt -o out.srt       # 把按行宽切碎的条目合并回整句(翻译前用,启发式)
  python srt_tools.py split in.srt -o out.srt       # 把多句粘连的超长条目按句末标点拆成一句一条(翻译前用)
  python srt_tools.py normalize in.srt -o out.srt   # 解析+行合并+时间轴校验+重编号(修正阶段用)
  python srt_tools.py apply base.srt trans.txt -o out.srt -l <lang>  # 把「编号<TAB>译文」贴回 base 的时间轴
  python srt_tools.py clean in.srt -o out.srt -l <lang>  # normalize + 标点规范化(译文阶段用; -l 决定标点风格)
  python srt_tools.py resplit in.srt -o out.srt -l <lang>  # 把整句译文切回观看用分条(交付前用)
  python srt_tools.py check base.srt target.srt     # 对比条目数/时间轴/漏译(第 4、5 步完成标准)
  python srt_tools.py provenance orig.srt out.srt   # 查产物的时间点里有多少是插值的(交付前如实告知用户)
  python srt_tools.py --self-test
"""
import argparse
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

BRACKET_MAP = str.maketrans({"[": "(", "]": ")", "【": "(", "】": ")"})
ELLIPSIS_RE = re.compile(r"\.{2,}|。{2,}|‥+")
# 句中直接替换为空格的非成对标点;· 和 ・ 是人名分隔符,不在其列。破折号族含 —–―‒(ｰ 是片假名长音符号,属词内符号,不在其列)
MID_PUNCT = set(",，、。．.;；:：~～—–―‒")
# CJK 全角标点子集:western 风格保留 ASCII 标点与破折号族,但仍把残留的 CJK 全角标点转空格(译文不该出现这些)
CJK_MID_PUNCT = set("，、。．；：～")
# 夹在两个数字之间时保留的标点:小数点、时间冒号、千位分隔逗号(3.5 / 12:30 / 1,000)。
# 顿号 、 不在其列——它在中文里是列举分隔符,不是数字内部符号
NUM_INNER = set(".:,．：，")
OPEN = set('「『“‘《〈(（«‹"')
CLOSE = set('」』”’》〉)）»›"')
TERMINAL_KEEP = set("?？!！…")
# 说话人前缀(ASR --diarize 产出),两种形态都认:
#   冒号式 `名字: 正文` —— 要求冒号后至少一个空格,避免把 12:30 / 午後3:30 误判为前缀
#   方括号式 `[S01] 正文` —— mossland-asr 等的 diarization 标签。标签限 ASCII(字母开头),
#     以免把 `[笑] そうですね`、`[音楽] …` 这类事件描述误判成说话人;真名一律走冒号式
SPEAKER_RE = re.compile(
    r"^(?:\[([A-Za-z_][A-Za-z0-9_\- ]{0,23})\]|([^\s:：,，、。．!?！？…\[\]]{1,24})[:：]) +"
)
# 句末标点(merge 判断上一条是否已收句),其后可跟闭引号
SENT_END = set("。．.！!？?…")
# 整条只有一个音频事件/场景描述(如 [オープニングミュージック]),merge 时既不吞并也不被吞
EVENT_RE = re.compile(r"^[\[(（【][^\[\]()（）【】]*[\])）】]$")
# resplit 的换行禁则(禁則処理),与上游 ASR 的 output.py 同口径:不可行首 / 不可行末的字符
NO_LINE_START = frozenset(
    "ーｰ〜"                       # 长音符
    "ぁぃぅぇぉっゃゅょゎゕゖ"      # 小书平假名
    "ァィゥェォッャュョヮヵヶ"      # 小书片假名
    "、。，．,.!！?？:：;；…‥・"    # 标点
    "）］｝」』】〉》〕”’\"'"       # 闭合括号与引号
    ")]}"
    "%％‰℃°"                     # 后置单位
)
NO_LINE_END = frozenset("（［｛「『【〈《〔“‘\"'$￥#＄￥＃" "([{")
# 无空格语言里的词边界近似:脚本类转换处更可能是词首。数值越低越适合断开
SCRIPT_BREAK_COST = {
    ("hira", "kanji"): 1.0,
    ("hira", "kata"): 1.0,
    ("kata", "kanji"): 1.5,
    ("kanji", "kata"): 1.5,
    ("hira", "hira"): 3.0,
    ("kanji", "kanji"): 4.0,  # 连续汉字多半是一个复合词
    ("kanji", "hira"): 4.0,   # 后面的假名多半是送り仮名
    ("kata", "hira"): 4.0,
    ("kata", "kata"): 6.0,    # 连续片假名是单个外来语
}
# 中文的词边界近似。日语靠假名/汉字交替就能估出词首,中文一整句全是汉字,
# ("kanji","kanji") 对每个位置给出同一个代价,脚本只能按行宽硬切,必然劈词
# (实测把「旅游景点」切成「景」/「点」、「公共小便池」切成「小便」/「池」)。
# 退而求其次靠高频虚词定位:代价压到略低于 4.0,使切点在均分目标前后约 4~5 个字的范围内
# 优先落到虚词边界上,但不至于为了迁就虚词把两段切得长短悬殊。
ZH_BREAK_AFTER = frozenset("的地得了着过们吗呢吧啊")   # 其后断开:「的地得」是修饰语标记,其后必是中心语开头
ZH_BREAK_BEFORE = frozenset("是在把被让使从对向给与和但而就也都还又很更最不没有为")  # 其前断开:多半是新谓语/介词短语的开头
# 上表按虚词用法立规,但这几个字也当实词用,那时其后断开就是劈词(「地方」「地下」「得到」)
ZH_AFTER_EXCEPT = {
    "地": frozenset("方下区上带球图位点址面基产形貌质"),
    "得": frozenset("到出力知"),
    "着": frozenset("急想眼手陆火凉"),
}
ZH_BREAK_AFTER_COST = 3.0
ZH_BREAK_BEFORE_COST = 3.2
# 中英混排的排版空格不是读点,只是排版约定,但它和 clean 转出来的真读点在文本里长得一模一样。
# 译文保留拉丁专名时(plaskrul / Urilift / ASML / Skillshare),这种空格能占到候选切点的一半,
# 若按 0.0 当完美读点用就会压倒均分目标,切出「而 krul」/「的设计…」这种 3 字孤儿行。
# 抬价而非禁止:没有更好的切点时它仍然可用,只是不再无条件胜出
MIXED_SPACE_COST = 1.5
# CJK 正文里连续的拉丁词几乎总是一个专名(「Shima Bulgariya」),词内空格同样不是读点
LATIN_SPACE_IN_CJK_COST = 2.5
# 混排空格左侧若是这些字,右边的拉丁词是它的中心语或被修饰对象,分家就成了断词:
# 「这个 plaskrul」「某些 Urilift」「最早的法式 pissoir」
CJK_GLUE_BEFORE_LATIN = frozenset("的地得着了个些种式型款位名台条只把新老每该此")
# 混排空格右侧若是这些字,它是左边拉丁词的黏着成分,同样不能分家:「krul 的设计」
CJK_GLUE_AFTER_LATIN = frozenset("的地得着了们")
# 每浪费一整行宽度所折算的切点代价。调高=更倾向塞满行宽,调低=更倾向找好切点
WASTE_WEIGHT = 4.0
# resplit 的单行列宽预算。上游 ASR 的 28 是「两行中的一行」,而本规范要求一条一行,
# 故 cjk 放宽到 38 列(≈19 个汉字,单行中文字幕的常见上限);拉丁保持 42
CJK_LINE_WIDTH = 38
LATIN_LINE_WIDTH = 42
# split/stats 的「超长条目」判据:一条整句 resplit 后不该超过三行,超过说明它是多句粘连
LONG_ENTRY_FACTOR = 3
LONG_ENTRY_SEC = 12.0
# stats 判定「被行宽切碎」的比例阈值:未收句条目占比超过它就该跑 merge
UNFINISHED_RATIO = 0.3


def _ms(h, m, s, ms):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def _fmt(t):
    h, r = divmod(t, 3600000)
    m, r = divmod(r, 60000)
    s, ms = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _is_cjk(ch):
    return any(a <= ord(ch) <= b for a, b in (
        (0x3000, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
        (0xF900, 0xFAFF), (0xAC00, 0xD7AF), (0xFF00, 0xFFEF),
    ))


def _is_punct(ch):
    return unicodedata.category(ch).startswith("P") or ch in "~～"


def _cw(ch):
    """单字符的显示列宽:全角/宽字符算 2 列,其余 1 列(与上游 ASR 的行宽口径一致)。"""
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _width(text):
    return sum(_cw(ch) for ch in text)


def _split_speaker(text):
    """拆出说话人前缀,返回(规范化前缀, 正文);无前缀则 ('', text)。

    前缀含尾随空格,可直接与正文拼接。两种形态各自规范化、不互转
    (形态互转由 `speakers` 子命令显式做):
      '丸岡和佳奈: 本日の' -> ('丸岡和佳奈: ', '本日の')
      '[S01] りすLOG'      -> ('[S01] ', 'りすLOG')
    """
    m = SPEAKER_RE.match(text)
    if not m:
        return "", text
    label, name = m.group(1), m.group(2)
    prefix = f"[{label}] " if label is not None else f"{name}: "
    return prefix, text[m.end():]


def _speaker_label(prefix):
    """前缀 -> 裸标签:'[S01] ' -> 'S01';'関根瞳: ' -> '関根瞳';'' -> ''。"""
    return prefix.strip().strip("[]").rstrip(":：").strip()


def _with_speaker(speaker, body):
    return f"{speaker}{body}" if speaker and body else body


def _ends_sentence(body):
    """正文是否已经收句(末尾是句末标点,允许其后跟闭引号)。"""
    t = body.rstrip()
    while t and t[-1] in CLOSE:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in SENT_END


def _has_inner_punct(body):
    """正文内部(剥掉末尾的句末标点/闭引号后)还有没有任何标点。

    用来区分两种「超长且只有一句」的条目,它们的处置完全相反:
      内部零标点 -> ASR 把句读整段吃了(实测过 462 列不带一个标点),补上句读再 `split`;
      内部有标点 -> ASR 的标点是好的,这就是一个真正的长句(英语单句 150~200 字符很常见),
                    不能硬插句号去切它,留给交付前的 `resplit` 按行宽折行。
    """
    t = body.rstrip()
    while t and (t[-1] in SENT_END or t[-1] in CLOSE):
        t = t[:-1].rstrip()
    return any(_is_punct(ch) for ch in t)


# 稀疏标点风格的 CJK 目标语言;其余(拉丁/西里尔等)走 western 风格,保留句中标点
CJK_LANGS = {"zh", "ja", "ko"}


def _style_for(lang):
    """根据目标语言决定 clean 的标点风格。缺省视为 cjk,保持向后兼容。"""
    if not lang:
        return "cjk"
    base = lang.lower().replace("_", "-").split("-")[0]
    return "cjk" if base in CJK_LANGS else "western"


def _join_lines(lines):
    out = lines[0]
    for nxt in lines[1:]:
        if out and nxt and _is_cjk(out[-1]) and _is_cjk(nxt[0]):
            out += nxt
        else:
            out += " " + nxt
    return out


def parse(text):
    entries = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if TIME_RE.search(l)), None)
        if ti is None:
            continue
        g = TIME_RE.search(lines[ti]).groups()
        content = [l.strip() for l in lines[ti + 1:] if l.strip()]
        if not content:
            continue
        entries.append({"start": _ms(*g[:4]), "end": _ms(*g[4:]), "text": _join_lines(content)})
    return entries


def clean_text(text, style="cjk"):
    # 说话人前缀原样透传:不参与句中标点替换,也不参与首尾标点剥离(否则 `丸岡和佳奈: ` 的冒号会被转成空格)
    speaker, text = _split_speaker(text)
    text = text.translate(BRACKET_MAP)
    text = ELLIPSIS_RE.sub("…", text)
    if style != "western":
        # cjk 风格:句中非成对标点转空格;数字/时间里的 . : ． ： 保留(str.isdigit 已覆盖全角数字 ０-９)
        chars = list(text)
        for i, ch in enumerate(chars):
            if ch in MID_PUNCT:
                prev = chars[i - 1] if i else ""
                nxt = chars[i + 1] if i + 1 < len(chars) else ""
                if ch in NUM_INNER and prev.isdigit() and nxt.isdigit():
                    continue  # 3.5 / 12:30 / 1,000 / ３．５ / １２：３０
                chars[i] = " "
        text = "".join(chars)
    else:
        # western 风格:保留 ASCII 标点与破折号族,但仍把残留的 CJK 全角标点转空格(译文不该出现这些)
        chars = list(text)
        for i, ch in enumerate(chars):
            if ch in CJK_MID_PUNCT:
                prev = chars[i - 1] if i else ""
                nxt = chars[i + 1] if i + 1 < len(chars) else ""
                if ch in NUM_INNER and prev.isdigit() and nxt.isdigit():
                    continue  # ３．５ / １２：３０ / １，０００
                chars[i] = " "
        text = "".join(chars)
    # 统一空白规范化(两种风格都需要,western 也要 collapse 首尾/多空格/制表符)
    text = re.sub(r"\s+", " ", text).strip()
    # 前导标点剥离(开引号/开括号除外)
    while text and _is_punct(text[0]) and text[0] not in OPEN:
        text = text[1:].lstrip()
    # 尾标点剥离,仅保留句末允许的标点;western 风格额外保留句号 .
    terminal = TERMINAL_KEEP if style != "western" else TERMINAL_KEEP | {"."}
    while text and _is_punct(text[-1]) and text[-1] not in terminal and text[-1] not in CLOSE:
        text = text[:-1].rstrip()
    # 正文清空时整条作废(不留只有 `说话人: ` 的空壳),由 process 过滤
    return _with_speaker(speaker, text)


def merge_entries(entries, max_gap_ms, max_merged_ms):
    """把 ASR 按行宽切碎的条目合并回整句(启发式,主代理需抽查)。

    合并到上一条的条件(全部满足):上一条未收句 + 说话人前缀相同 + 间隔 <= max_gap +
    合并后总时长 <= max_merged_duration。整条只有音频事件的条目不参与合并。
    """
    out = []
    for e in entries:
        sp, body = _split_speaker(e["text"])
        is_event = bool(EVENT_RE.match(body.strip()))
        if out:
            p = out[-1]
            if (not p["_event"] and not is_event
                    and not _ends_sentence(p["_body"])
                    and p["_sp"] == sp
                    and e["start"] - p["end"] <= max_gap_ms
                    and e["end"] - p["start"] <= max_merged_ms):
                parts = [x for x in (p["_body"], body) if x]
                p["_body"] = _join_lines(parts) if parts else ""
                p["end"] = max(p["end"], e["end"])
                continue
        out.append({"start": e["start"], "end": e["end"], "_sp": sp, "_body": body, "_event": is_event})
    for e in out:
        e["text"] = _with_speaker(e["_sp"], e["_body"])
    return out


def _script_of(ch):
    """粗粒度字符类,用于在无空格语言里估计词边界。口径与上游 ASR 的 output.py 一致。"""
    if "ぁ" <= ch <= "ゟ":
        return "hira"
    if "゠" <= ch <= "ヿ" or "ｦ" <= ch <= "ﾝ":
        return "kata"
    if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
        return "kanji"
    if ch.isascii() and ch.isalnum():
        return "latin"
    return "other"


def _cut_cost(text, i, zh=False):
    """把 text 切成 text[:i] / text[i:] 的代价,越低越好;None = 禁止在此切。

    日语/中文没有空格,脚本类转换代价近似词边界:假名后接汉字通常是新词开头,
    而汉字后接假名多半是同一个词的送り仮名,不能切。
    zh=True 时额外启用中文虚词边界(见 ZH_BREAK_AFTER/BEFORE)——中文没有假名可依,
    汉字-汉字处处同价,不给点线索就只能硬切劈词。只对中文开,免得误伤日语里的同形汉字。
    """
    if i <= 0 or i >= len(text):
        return None
    prev, nxt = text[i - 1], text[i]
    if nxt in NO_LINE_START or prev in NO_LINE_END:
        return None  # 禁则:闭合符号/小书假名/长音符不可行首,开括号不可行末
    if prev == " " or nxt == " ":
        # 空格通常是完美切点(clean 已把句中标点转成空格,即天然读点),但混排排版空格不是:
        # 它长得和读点一模一样,却只是 CJK 与拉丁字母/数字之间的排版约定。详见 MIXED_SPACE_COST
        j, k = i - 1, i
        while j >= 0 and text[j] == " ":
            j -= 1
        while k < len(text) and text[k] == " ":
            k += 1
        if j < 0 or k >= len(text):
            return 0.0
        a, b = text[j], text[k]
        a_cjk, b_cjk = _is_cjk(a), _is_cjk(b)
        if a_cjk == b_cjk:
            # 两侧同类。CJK-CJK 是 clean 留下的真读点;拉丁-拉丁在 CJK 正文里多半是专名内部
            # (「Shima Bulgariya」「St. Andrew」),不该按完美读点切开。
            # 英文字幕的词间空格不受影响——这一条只在正文含 CJK 时生效
            if not a_cjk and any(_is_cjk(c) for c in text):
                return LATIN_SPACE_IN_CJK_COST
            return 0.0
        if a_cjk:  # CJK 在左、拉丁在右:「这个 plaskrul」「某些 Urilift」「法式 pissoir」
            if a in CJK_GLUE_BEFORE_LATIN:
                return None
        else:      # 拉丁/数字在左、CJK 在右:「1000 名」「krul 的设计」
            if a.isdigit() or b in CJK_GLUE_AFTER_LATIN:
                return None
        return MIXED_SPACE_COST
    a, b = _script_of(prev), _script_of(nxt)
    if a == "latin" and b == "latin":
        return None  # 不切开拉丁词
    if zh and a == "kanji" and b == "kanji":
        if prev in ZH_BREAK_AFTER and nxt not in ZH_AFTER_EXCEPT.get(prev, ()):
            return ZH_BREAK_AFTER_COST
        if nxt in ZH_BREAK_BEFORE:
            return ZH_BREAK_BEFORE_COST
    return SCRIPT_BREAK_COST.get((a, b), 2.0)


def _split_body(body, budget, zh=False):
    """按显示列 budget 把正文切成若干段,切点取代价最低者。

    段数由 budget 定死(ceil(总宽/budget)),但每段的目标宽度是**均分**后的宽度,不是塞满 budget。
    贪心塞满会留下一两个字的孤儿尾段——40 列的句子按 38 列贪心会切成 38+2,末条只剩一个字,
    既难看又几乎必然把词劈开(候选切点全挤在行尾那一两个字附近,挑无可挑)。
    均分后切点候选散布在句子中部,命中标点/词边界的机会大得多,两段长度也匀。
    """
    if _width(body) <= budget:
        return [body]
    segs, rest = [], body
    while _width(rest) > budget:
        cut, w = 0, 0
        for i, ch in enumerate(rest):
            if w + _cw(ch) > budget:
                break
            w += _cw(ch)
            cut = i + 1
        cut = max(cut, 1)  # budget 小于单字宽时兜底,防死循环
        # 剩余部分还要切成几段,以及均分后每段的目标宽度(硬上界仍是 budget)
        rest_w = _width(rest)
        target = rest_w / max(1, -(-rest_w // budget))
        lo = max(1, cut // 3)
        best, best_score = None, None
        for i in range(cut, lo - 1, -1):
            c = _cut_cost(rest, i, zh)
            if c is None:
                continue
            # 在切点质量与「贴近均分目标」之间权衡:好切点值得偏离目标,但不值得偏太多
            score = c + abs(_width(rest[:i].rstrip()) - target) / budget * WASTE_WEIGHT
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is None:
            best = cut  # 候选全被禁则挡住,兜底硬切
        segs.append(rest[:best].rstrip())
        rest = rest[best:].lstrip()
    if rest:
        segs.append(rest)
    return [s for s in segs if s]


def _alloc(start, end, segs, min_dur_ms=0):
    """在 [start, end] 内按各段显示列数比例分配时间轴,首尾对齐原区间、段间不重叠。

    带 min_dur_ms 下界时用 water-filling:不足下界的段钉到下界,剩余时间在其余段按宽度重分,
    迭代至稳定 —— 即短段从长段借时间,而不是牺牲行宽把段合并回去。
    区间本身装不下 n 段下界时(异常输入)退化为均分。
    """
    span, n = end - start, len(segs)
    weights = [max(1, _width(s)) for s in segs]
    if min_dur_ms * n > span:
        durs = [span // n] * n
    else:
        pinned = [False] * n
        while True:
            free = span - min_dur_ms * sum(pinned)
            fw = sum(w for w, p in zip(weights, pinned) if not p)
            durs = [min_dur_ms if p else free * w // fw for w, p in zip(weights, pinned)]
            short = [i for i in range(n) if not pinned[i] and durs[i] < min_dur_ms]
            if not short:
                break
            for i in short:
                pinned[i] = True
    times, t = [], start
    for i, d in enumerate(durs):
        nxt = end if i == n - 1 else min(t + d, end)
        times.append((t, nxt))
        t = nxt
    return times


def _sentences(body):
    """按句末标点把正文切成若干句(标点留在句尾,其后的闭引号并入本句)。

    引号/括号内部的句号不断句(引用别人说的整句话是一句),数字里的 . 也不算句末。
    """
    out, buf, i, n, depth = [], "", 0, len(body), 0
    while i < n:
        ch = body[i]
        buf += ch
        i += 1
        if ch in OPEN and ch != '"':  # ASCII 直引号开闭同形,不参与配对计数
            depth += 1
        elif ch in CLOSE and ch != '"':
            depth = max(0, depth - 1)
        if ch not in SENT_END or depth:
            continue
        if ch in ".．" and len(buf) >= 2 and buf[-2].isdigit() and i < n and body[i].isdigit():
            continue  # 3.5 / １．５ 不是句末
        while i < n and (body[i] in SENT_END or body[i] in CLOSE):
            buf += body[i]  # 连续句末标点(!?、……)与随后的闭引号并入本句
            i += 1
        if i < n:  # 后面还有内容才断句
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def split_entries(entries, max_width, max_dur_ms, min_dur_ms, report=None):
    """merge 的反向操作:把多句粘连的超长条目按句末标点拆成一句一条,时间轴按各句显示宽度比例分配。

    ASR 的分段依据是静音(VAD),说话人不停顿时十几句会被打包成一条、只在末尾补一个句号,
    这类条目在原文层就该拆开——原文字符数≈音节数≈时长,比例分配可靠;留到译文层的 resplit
    再切,分配依据变成译文字宽,跨语言字数比不恒定,时间轴会漂移。

    只有超出宽度或时长阈值的条目才拆(短的多句条目留在一起,翻译时上下文更完整);
    内部没有句末标点可切的超长条目原样保留并计入 report["stuck"],元组为
    (产物里的条目号, 时长ms, 显示列宽, 内部是否有标点)——最后一项区分「ASR 吃了句读、补上就能切」
    与「本来就是一个长句、不该动」,详见 `_has_inner_punct`。
    report 里的条目号是**产物**里的号(拆分会把后面的条目顺推),主代理拿着它直接去编辑产物。
    """
    out, done, stuck = [], [], []
    for e in entries:
        sp, body = _split_speaker(e["text"])
        over = _width(body) > max_width or e["end"] - e["start"] > max_dur_ms
        segs = _sentences(body) if over else [body]
        if not over or len(segs) <= 1:
            if over:
                stuck.append((len(out) + 1, e["end"] - e["start"], _width(body), _has_inner_punct(body)))
            out.append(e)
            continue
        done.append((len(out) + 1, len(segs)))
        for s, (a, b) in zip(segs, _alloc(e["start"], e["end"], segs, min_dur_ms)):
            out.append({"start": a, "end": b, "text": _with_speaker(sp, s)})
    if report is not None:
        report["done"], report["stuck"] = done, stuck
    return out


def resplit_entries(entries, budget, min_dur_ms, zh=False):
    """把整句译文切回观看用分条:每段不超行宽预算,说话人前缀每条都带且不占预算。"""
    out = []
    for e in entries:
        sp, body = _split_speaker(e["text"])
        segs = _split_body(body, budget, zh) if body else [body]
        if len(segs) <= 1:
            out.append(e)
            continue
        for s, (a, b) in zip(segs, _alloc(e["start"], e["end"], segs, min_dur_ms)):
            out.append({"start": a, "end": b, "text": _with_speaker(sp, s)})
    return out


def speaker_counts(entries):
    """按出现顺序统计说话人前缀:[(前缀, 条目数)];无前缀的条目计在 '' 名下。"""
    counts = {}
    for e in entries:
        sp = _split_speaker(e["text"])[0]
        counts[sp] = counts.get(sp, 0) + 1
    return list(counts.items())


def rewrite_speakers(entries, mapping=None, drop=False):
    """落实说话人前缀:按 mapping 把占位标签换成真名(统一为 `名字: ` 形态),或 drop 掉全部前缀。

    mapping 的键写裸标签(`S01`)或完整前缀(`[S01] `)都行。未被 mapping 命中的前缀原样保留,
    连同命中次数一起返回,由调用方回报。
    """
    mapping = {_speaker_label(k): v for k, v in (mapping or {}).items()}
    unmapped = [sp for sp, _ in speaker_counts(entries)
                if sp and not drop and _speaker_label(sp) not in mapping]
    hit = {}
    for e in entries:
        sp, body = _split_speaker(e["text"])
        if not sp:
            continue
        if drop:
            e["text"] = body
            hit[sp] = hit.get(sp, 0) + 1
            continue
        name = mapping.get(_speaker_label(sp))
        if name:
            e["text"] = _with_speaker(f"{name}: ", body)
            hit[sp] = hit.get(sp, 0) + 1
    return hit, unmapped


def speakers(path, output=None, mapping=None, drop=False):
    """列出/改写说话人前缀。不带 --map/--drop 时只列出,不写文件。返回退出码 0。"""
    entries = process(Path(path).read_text(encoding="utf-8-sig"), "normalize")
    counts = speaker_counts(entries)
    labeled = [(sp, n) for sp, n in counts if sp]
    print(f"speakers: {len(labeled)} prefix(es) over {len(entries)} entries")  # ponytail: ASCII 输出
    for sp, n in counts:
        print(f"  {sp.rstrip() if sp else '(no prefix)'}  x{n}")
    if not mapping and not drop:
        if labeled:
            print("  -> rename with `speakers <file> --map S01=関根瞳`, "
                  "or strip with `--drop` (single-speaker subtitles)")
        return 0
    hit, unmapped = rewrite_speakers(entries, mapping, drop)
    out = output or path
    Path(out).write_text(serialize(entries), encoding="utf-8", newline="\n")
    action = "dropped" if drop else "renamed"
    print(f"OK: {action} {sum(hit.values())} entries -> {out}")
    if unmapped:
        print(f"  WARN prefix left untouched (no mapping): {', '.join(s.rstrip() for s in unmapped)}")
    return 0


# 译文行:`编号<TAB>译文`。分隔符容忍 tab / 竖线 / 冒号 / 空格,译文里的数字不受影响
TRANS_LINE_RE = re.compile(r"^\s*(\d+)\s*[\t|:：]?[ \t]*(.*?)\s*$")


def parse_translation(text, expected_n):
    """解析「编号<TAB>译文」文本,返回 {编号: 译文}。编号必须恰好覆盖 1..expected_n。

    翻译子代理只写译文、不写时间轴——时间轴由 apply 从原文搬过来,LLM 无从写错。
    代价是漏行/多行会错位,所以这里把编号完整性当硬约束校验,出问题直接指名条目号。
    """
    rows, malformed = {}, []
    for ln, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if not line.strip():
            continue
        m = TRANS_LINE_RE.match(line)
        if not m or not m.group(2):
            malformed.append((ln, line.strip()[:40]))
            continue
        n = int(m.group(1))
        if n in rows:
            malformed.append((ln, f"duplicate index #{n}"))
            continue
        rows[n] = m.group(2)
    expected = set(range(1, expected_n + 1))
    return rows, malformed, sorted(expected - set(rows)), sorted(set(rows) - expected)


def apply_translation(base_path, trans_path, out_path, lang=None):
    """把译文文本贴回 base 的时间轴。返回退出码:0=成功,1=译文有缺漏。"""
    base = process(Path(base_path).read_text(encoding="utf-8-sig"), "normalize")
    style = _style_for(lang)
    rows, malformed, missing, extra = parse_translation(
        Path(trans_path).read_text(encoding="utf-8-sig"), len(base))
    ok = True
    print(f"base={len(base)} entries, translation={len(rows)} lines")  # ponytail: ASCII 输出
    if malformed:
        detail = "; ".join(f"line {ln}: {s}" for ln, s in malformed[:10])
        print(f"  ERROR {len(malformed)} malformed line(s), expected `<index><TAB><text>`: {detail}")
        ok = False
    if missing:
        print(f"  ERROR {len(missing)} entries have no translation: {_preview(missing)}")
        ok = False
    if extra:
        print(f"  ERROR {len(extra)} indexes out of range 1..{len(base)}: {_preview(extra)}")
        ok = False
    if not ok:
        print("  -> fix the translation file and run `apply` again; nothing was written")
        return 1
    entries = [{"start": e["start"], "end": e["end"], "text": clean_text(rows[i + 1], style)}
               for i, e in enumerate(base)]
    blanked = [i + 1 for i, e in enumerate(entries) if not e["text"]]
    if blanked:
        print(f"  ERROR translation is punctuation-only at {len(blanked)} entries: {_preview(blanked)}")
        print("  -> nothing was written")
        return 1
    Path(out_path).write_text(serialize(entries), encoding="utf-8", newline="\n")
    print(f"OK: {len(entries)} entries -> {out_path}")
    print("  timeline copied from base (the translator never writes timestamps)")
    return 0


def provenance(orig_path, derived_path):
    """报告 derived 的时间点里哪些来自原始 ASR、哪些是脚本插值出来的。返回 0=插值都在区间内,1=有越界。

    `split` 和 `resplit` 都会在原条目区间内按字宽比例插入新的切点——这些点是算出来的,
    **没有音频依据**。交付前必须拿这个数字如实告知用户,让他决定要哪一份。
    """
    orig = parse(Path(orig_path).read_text(encoding="utf-8-sig"))
    derived = parse(Path(derived_path).read_text(encoding="utf-8-sig"))
    real = {t for e in orig for t in (e["start"], e["end"])}
    pts = sorted({t for e in derived for t in (e["start"], e["end"])})
    interp = [t for t in pts if t not in real]
    outside = [t for t in interp if not any(e["start"] < t < e["end"] for e in orig)]
    print(f"original: {len(orig)} entries, {len(real)} distinct time points")  # ponytail: ASCII 输出
    print(f"derived:  {len(derived)} entries, {len(pts)} distinct time points")
    print(f"  from the original ASR: {len(pts) - len(interp)}")
    print(f"  interpolated (no audio evidence): {len(interp)}")
    if outside:
        print(f"  ERROR {len(outside)} interpolated points fall outside every original entry span: "
              + ", ".join(_fmt(t) for t in outside[:10]))
        print("    -> a real ASR boundary was moved; this must not happen")
        return 1
    print("  all interpolated points fall strictly inside an original entry span (no real boundary moved)")
    return 0


def check(base_path, target_path, fix_timeline=False):
    """对比两份 SRT 的条目对齐情况。返回退出码:0=一致,1=有差异。

    fix_timeline=True 时,条目数相同的前提下用 base 的时间轴覆盖 target 并写回
    ——时间轴本就该逐条相等,错位一定是抄错,没有第二种解释。
    """
    base = parse(Path(base_path).read_text(encoding="utf-8-sig"))
    target = parse(Path(target_path).read_text(encoding="utf-8-sig"))
    n = min(len(base), len(target))
    ts_bad = [i + 1 for i in range(n)
              if abs(base[i]["start"] - target[i]["start"]) > 1 or abs(base[i]["end"] - target[i]["end"]) > 1]
    if ts_bad and fix_timeline and len(base) == len(target):
        for i in range(n):
            target[i]["start"], target[i]["end"] = base[i]["start"], base[i]["end"]
        Path(target_path).write_text(serialize(target), encoding="utf-8", newline="\n")
        print(f"FIXED timeline at {len(ts_bad)} entries from base: {_preview(ts_bad)}")
        ts_bad = []
    empty = [i + 1 for i, e in enumerate(target) if not _split_speaker(e["text"])[1].strip()]
    identical = [i + 1 for i in range(n) if base[i]["text"] == target[i]["text"]]
    print(f"base={len(base)} entries, target={len(target)} entries")  # ponytail: ASCII 输出
    ok = True
    if len(base) != len(target):
        print(f"  DIFF entry count: {len(base)} -> {len(target)} (diff {len(target) - len(base):+d})")
        ok = False
    if ts_bad:
        print(f"  DIFF timeline mismatch at {len(ts_bad)} entries: {_preview(ts_bad)}")
        ok = False
    if empty:
        print(f"  DIFF empty body at {len(empty)} entries: {_preview(empty)}")
        ok = False
    if identical:
        # 仅提示:日中翻译里纯汉字词/专名合法相同,但大量相同通常意味着漏译
        print(f"  NOTE identical to base at {len(identical)} entries (possible untranslated): {_preview(identical)}")
    if ok:
        print("OK: aligned")
    return 0 if ok else 1


def _preview(nums, limit=20):
    head = ", ".join(f"#{n}" for n in nums[:limit])
    return head + (f", ... (+{len(nums) - limit} more)" if len(nums) > limit else "")


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def stats(path, lang=None, max_width=None, max_dur=LONG_ENTRY_SEC):
    """体检输入 SRT 的条目形态,判定该跑 merge(被行宽切碎)还是 split(多句粘连)。返回退出码 0。

    ASR 的两种失形是相反的:按显示行宽切的会把一句拆成三四条(多数条目不收句),
    按静音切的会把十几句打包成一条(条目收句、但又长又挤)。两者都要在翻译前整平。
    """
    entries = process(Path(path).read_text(encoding="utf-8-sig"), "normalize")
    style = _style_for(lang)
    if not max_width:
        max_width = LONG_ENTRY_FACTOR * (CJK_LINE_WIDTH if style == "cjk" else LATIN_LINE_WIDTH)
    max_dur_ms = int(max_dur * 1000)
    n = len(entries)
    bodies = [_split_speaker(e["text"])[1] for e in entries]
    durs = [e["end"] - e["start"] for e in entries]
    widths = [_width(b) for b in bodies]
    sents = [len(_sentences(b)) for b in bodies]
    unfinished = [i + 1 for i, b in enumerate(bodies) if not _ends_sentence(b)]
    long_ = [i + 1 for i in range(n) if widths[i] > max_width or durs[i] > max_dur_ms]
    splittable = [i for i in long_ if sents[i - 1] >= 2]
    # 超长且只有一句的条目分两种,处置相反(见 _has_inner_punct):
    #   nopunct — 内部一个标点都没有,ASR 把句读吃了,补上再 split
    #   oneline — 内部有标点,是一个已经标好的长句,不能硬插句号,留给 resplit
    stuck = [i for i in long_ if sents[i - 1] <= 1]
    nopunct = [i for i in stuck if not _has_inner_punct(bodies[i - 1])]
    oneline = [i for i in stuck if _has_inner_punct(bodies[i - 1])]
    print(f"entries={n}  total={_fmt(max(e['end'] for e in entries))}")  # ponytail: ASCII 输出
    print(f"duration/entry  min={min(durs) / 1000:.1f}s  median={_median(durs) / 1000:.1f}s  "
          f"max={max(durs) / 1000:.1f}s  (over {max_dur:.0f}s: {sum(d > max_dur_ms for d in durs)})")
    print(f"width/entry     min={min(widths)}  median={_median(widths):.0f}  "
          f"max={max(widths)} col  (over {max_width}: {sum(w > max_width for w in widths)})")
    ratio = len(unfinished) / n
    print(f"ends a sentence: {n - len(unfinished)}/{n} ({1 - ratio:.0%})")
    inner_n = sum(1 for b in bodies if _has_inner_punct(b))
    inner_ratio = inner_n / n
    print(f"punctuation inside: {inner_n}/{n} ({inner_ratio:.0%})"
          "  -- a low ratio means the ASR is dropping punctuation")
    order = sorted(range(n), key=lambda i: -durs[i])[:5]
    print("longest: " + ", ".join(
        f"#{i + 1} ({durs[i] / 1000:.1f}s, {widths[i]}col, {sents[i]} sent)" for i in order))
    spk = speaker_counts(entries)
    labeled = [(sp, c) for sp, c in spk if sp]
    if labeled:
        detail = ", ".join(f"{sp.rstrip()} x{c}" for sp, c in spk if sp)
        bare = next((c for sp, c in spk if not sp), 0)
        print(f"speakers: {len(labeled)} prefix(es) -- {detail}"
              + (f", no prefix x{bare}" if bare else ""))
    print("VERDICT")
    if ratio >= UNFINISHED_RATIO:
        print(f"  merge: NEEDED -- {len(unfinished)}/{n} ({ratio:.0%}) entries do not end a sentence, "
              "i.e. one sentence is spread over several entries")
    else:
        print(f"  merge: not needed -- only {len(unfinished)}/{n} ({ratio:.0%}) entries do not end a sentence")
    if splittable or nopunct:
        need = sorted(splittable + nopunct)
        print(f"  split: NEEDED -- {len(need)} entries over {max_width}col or {max_dur:.0f}s "
              f"look like several sentences glued together: {_preview(need)}")
        if splittable:
            print(f"    {len(splittable)} have sentence punctuation inside, `split` cuts them right away: "
                  f"{_preview(splittable)}")
        if nopunct:
            print(f"    {len(nopunct)} have no punctuation inside at all -- the ASR dropped the sentence "
                  f"breaks: {_preview(nopunct)}")
            print("    -> add sentence punctuation to these while fixing the transcript, then run `split`")
            if inner_ratio >= 0.5:
                print(f"       (but {inner_ratio:.0%} of all entries DO have punctuation inside, so this ASR "
                      "does keep it -- read these by hand first,")
                print("        they may simply be single sentences short enough to need no comma)")
    else:
        print(f"  split: not needed -- no glued entry over {max_width}col or {max_dur:.0f}s")
    if oneline:
        print(f"  NOTE {len(oneline)} entries are over {max_width}col or {max_dur:.0f}s but are a single, "
              f"already-punctuated sentence: {_preview(oneline)}")
        print("       leave them as they are -- `resplit` wraps them at delivery time. Do NOT insert fake "
              "sentence breaks to make them shorter; that cuts the sentence up and hurts the translation.")
        print("       (common with latin-script sources: one English sentence of 150-200 chars is normal)")
    if len(labeled) == 1:
        print(f"  speakers: single speaker ({labeled[0][0].rstrip()}) -- strip the prefix in step 3b: "
              "`speakers <file> --drop`")
    elif labeled:
        print(f"  speakers: {len(labeled)} speakers -- put the real names in, in step 3b: "
              "`speakers <file> --map S01=NAME --map S02=NAME`")
    return 0


def process(text, mode, lang=None, **opts):
    entries = parse(text)
    if not entries:
        sys.exit("ERROR: no subtitle entries parsed")
    style = _style_for(lang)  # clean/split/resplit 模式用;normalize、merge 不受 style 影响
    entries.sort(key=lambda e: e["start"])
    for e in entries:
        if e["end"] < e["start"]:
            e["start"], e["end"] = e["end"], e["start"]
        if mode == "clean":
            e["text"] = clean_text(e["text"], style)
    if mode == "merge":
        entries = merge_entries(entries, int(opts["max_gap"] * 1000), int(opts["max_merged_duration"] * 1000))
    elif mode == "split":
        max_width = opts.get("max_width") or LONG_ENTRY_FACTOR * (
            CJK_LINE_WIDTH if style == "cjk" else LATIN_LINE_WIDTH)
        entries = split_entries(entries, max_width, int(opts["max_duration"] * 1000),
                                int(opts["min_duration"] * 1000), opts.get("report"))
    elif mode == "resplit":
        budget = opts.get("max_line_width") or (CJK_LINE_WIDTH if style == "cjk" else LATIN_LINE_WIDTH)
        zh = (lang or "").lower().replace("_", "-").split("-")[0] == "zh"
        entries = resplit_entries(entries, budget, int(opts["min_duration"] * 1000), zh)
    return [e for e in entries if e["text"]]


def serialize(entries):
    return "\n".join(
        f"{i}\n{_fmt(e['start'])} --> {_fmt(e['end'])}\n{e['text']}\n"
        for i, e in enumerate(entries, 1)
    )


AGENTS_MD_TEMPLATE = """# {stem} 字幕翻译工作区

本目录是 `{stem}.srt` 的翻译工作区。外部原始字幕保持不动，所有产出都在本目录内。

## 文件布局

- `{stem}.srt` — 原始字幕副本（不要直接改，改带后缀的产物）
- `{stem}_merged.srt` — `merge` 产物：按行宽切碎的条目合并回整句（输入本来就一句一条时没有这个文件）
- `{stem}_fix.srt` — 转录修正稿（原语言，一条一整句，纠正听录错误；`split` 原地作用于它）
- `{stem}_<lang>.txt` — 翻译子代理的原始输出：一行一条的 `编号<TAB>译文`，不含时间轴
- `{stem}_<lang>.srt` — 翻译初稿（`apply` 把上面那份贴回 `_fix.srt` 的时间轴生成）
- `{stem}_<lang>_fix.srt` — 复核稿
- `{stem}_<lang>_split.srt` — 重切分终稿，**交付给用户的就是这一份**
- `AGENTS.md` — 本文件，说明结构与流程
- `_context/` — 背景资料区
  - `hits.json` — 知识库命中清单（`kb_tools.py match`）：库里已有哪些名字、ASR 错听在哪几条、未覆盖的候选词
  - `alias_log.tsv` — 别名替换日志：源时间段、原文、替换状态；整平后用 `remap-log` 更新 `current_entries`
  - `gaps.md` — 缺口清单：库里没有、需要调研的项。补缺调研只查这上面的
  - `brief.md` — 背景简报：内容概述、专名、疑似听录错误、引述段落、风格基调。修正/翻译/复核前都要读。
  - `glossary.md` — 术语表：「来自知识库」一节由脚本生成，「本次新增」由主代理填。翻译/复核必须遵循。
  - `research/` — 补缺调研产出（`01-gaps.md`…），每项带来源 URL。简报与术语表由它提炼而来。
  - `review_notes.md` — 复核修改清单（条目 / 原译 / 改后 / 类型 / 原因）。沉淀阶段读它
  - `sediment_proposal.json` — 沉淀提案；`sediment_result.md` — 落库摘要

## 流程

翻译全程走「一条一整句」：先把 ASR 的两种失形都整平——被行宽切碎的合并回整句（`merge`），
按静音粘连成一大坨的拆成一句一条（`split`）——再翻译（语义完整、时间轴对得上，译文质量最高），
最后一步才按目标语言行宽切回观看用分条。所以中间产物条目少而长，属正常。

0. 跑 `stats` 体检，看 `VERDICT` 判定该 `merge` 还是 `split`（或都不需要），以及有几个说话人
1. 建工作目录（已完成，由 `srt_tools.py init` 生成本文件与 `_context/` 占位）
2. 跑 `kb_tools.py match` 加载知识库、读 `index.md` 与命中领域包 → 问清原始/目标语言、视频日期、主题、各说话人是谁、是否调研 → 通读 `{stem}.srt` 写 `brief.md` 与 `gaps.md` → 派 1 个补缺调研子代理写 `_context/research/` → 跑 `kb_tools.py glossary` 生成术语表并填「本次新增」
3. 跑 `merge` → 复制为 `{stem}_fix.srt` → 跑 `speakers`（占位标签 `--map` 换真名，单说话人 `--drop` 去前缀）→ 跑 `kb_tools.py replace` 做别名替换 → 主代理对照简报/术语表**定点 Edit** 纠错（含 replace 列出的待确认项）、给超长条目补句读 → 跑 `split`（没跑则跑 `normalize`）→ `kb_tools.py remap-log {stem}_fix.srt --log _context/alias_log.tsv` 更新日志定位；简报按时间段定位，有附编号则更新为当前稿编号
4. 派翻译子代理（先读规范、translation-style、`brief.md`、`glossary.md`、领域 `style.md`）→ 只写 `编号<TAB>译文` 的 `{stem}_<lang>.txt`，主代理跑 `apply` 贴回时间轴 → `{stem}_<lang>.srt`，再跑 `check`
5. 派复核子代理（先复制再定点 Edit，写 `review_notes.md`）→ `{stem}_<lang>_fix.srt`，跑 `clean` + `check`（时间轴被改坏用 `check --fix-timeline` 直接覆盖）；主代理自己也要读一遍译文，别全信复核代理
6. 跑 `resplit` 切回观看用分条 → `{stem}_<lang>_split.srt`；跑 `provenance` 把插值数量告知用户，由用户定交付哪一份
7. 主代理抽查终稿，向用户报告产出路径、术语表、修正要点、插值点数
8. 派沉淀子代理写 `sediment_proposal.json` → 跑 `kb_tools.py apply` → 摘要给用户确认后 commit 知识库
"""

BRIEF_MD_TEMPLATE = """# 背景简报 — {stem}

> 占位文件。第 2 步调研完成后，由主代理根据 `_context/research/` 下的调研文件编辑替换本内容。

## 内容概述

（待填）

## 专名

（待填；标出哪些库里已有、哪些是本次新增）

## 疑似听录错误

（待填；别名表没覆盖的，附条目号与上下文）

## 引述段落

（待填；朗读来信、复述他人发言的起止时间与被引述者；可附条目号并注明所属文件）

## 风格基调

（待填；体裁；按起止时间标语域：旁白 / 新闻 / 街访 / 来信朗读；原文的梗在哪；整平后不能沿用旧编号）

## 外语插播段

（有则逐条列出原文与语义，无则写"无"）
"""

GLOSSARY_MD_TEMPLATE = """# 术语表 — {stem}

> 占位文件。第 2e 步由 `kb_tools.py glossary` 覆盖生成「来自知识库」一节，主代理再填「本次新增」。
> 格式：`原语言写法 → 目标语言标准译名`，查不到标"自拟"。

## 来自知识库

（待生成）

## 本次新增

（待填）
"""

GAPS_MD_TEMPLATE = """# 缺口清单 — {stem}

> 第 2c 步由主代理填写：只列知识库里没有的。每项一行：原文写法 / 出现条目号 / 你猜它是什么。
> 补缺调研子代理只查这里的项。为空或很少时建议用户跳过调研。

## 库里没有的人名 / 作品 / 节目 / 组织

（待填）

## 库里有但 volatile 可能过期的

（待填）

## 需要核实的近期事件

（待填）

## 疑似 ASR 错听但别名表没覆盖的

（待填）
"""


def init_workspace(original_srt):
    """在原 srt 同目录建 <stem>/ 工作区：复制原 srt、写 AGENTS.md、占位 brief/glossary、空 research/。"""
    src = Path(original_srt).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".srt":
        sys.exit(f"ERROR: not a valid .srt file: {src}")
    stem = src.stem
    workdir = src.parent / stem
    if workdir.exists():
        sys.exit(f"ERROR: workspace already exists (this script never overwrites): {workdir}")
    context = workdir / "_context"
    research = context / "research"
    workdir.mkdir()
    context.mkdir()
    research.mkdir()
    shutil.copy2(src, workdir / src.name)
    (workdir / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE.format(stem=stem), encoding="utf-8", newline="\n")
    (context / "brief.md").write_text(BRIEF_MD_TEMPLATE.format(stem=stem), encoding="utf-8", newline="\n")
    (context / "glossary.md").write_text(GLOSSARY_MD_TEMPLATE.format(stem=stem), encoding="utf-8", newline="\n")
    (context / "gaps.md").write_text(GAPS_MD_TEMPLATE.format(stem=stem), encoding="utf-8", newline="\n")
    print(f"OK: workspace created -> {workdir}")
    print(f"  - {workdir / src.name}")
    print(f"  - {workdir / 'AGENTS.md'}")
    print(f"  - {context / 'brief.md'} (placeholder)")
    print(f"  - {context / 'glossary.md'} (placeholder)")
    print(f"  - {context / 'gaps.md'} (placeholder)")
    print(f"  - {research} (empty)")


def self_test():
    # init 子命令：建临时 srt，跑 init，检查结构，再清理
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "how-to-code.srt"
        src.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
        init_workspace(str(src))
        wd = Path(d) / "how-to-code"
        assert (wd / "how-to-code.srt").read_text(encoding="utf-8").startswith("1\n")
        assert "how-to-code" in (wd / "AGENTS.md").read_text(encoding="utf-8")
        assert "待填" in (wd / "_context" / "brief.md").read_text(encoding="utf-8")
        assert "待填" in (wd / "_context" / "glossary.md").read_text(encoding="utf-8")
        assert (wd / "_context" / "research").is_dir()
        # 已存在应报错
        try:
            init_workspace(str(src))
            raise AssertionError("重复 init 应报错")
        except SystemExit:
            pass
    assert _join_lines(["えっと、", "それはね"]) == "えっと、それはね"
    assert _join_lines(["Hello", "world"]) == "Hello world"
    assert clean_text("、你好,世界。") == "你好 世界"
    assert clean_text("[笑]真的吗?") == "(笑)真的吗?"
    assert clean_text("Wait... what.") == "Wait… what"
    assert clean_text("现在是3.5版本,时间12:30。") == "现在是3.5版本 时间12:30"
    # 千位分隔逗号夹在数字之间要保住,否则 1,000 会被拆成 1 000
    assert clean_text("前 1,000 名可以免费试用。") == "前 1,000 名可以免费试用"
    assert clean_text("The first 1,000 people.", "western") == "The first 1,000 people."
    assert clean_text("先看这个,再看那个。") == "先看这个 再看那个"  # 非数字间的逗号照旧转空格
    assert clean_text("有1、2、3种。") == "有1 2 3种"  # 顿号是列举分隔符,不受数字保护,照旧转空格

    assert clean_text("现在是３．５版本,时间１２：３０。") == "现在是３．５版本 时间１２：３０"  # 全角数字/时间保护
    assert clean_text("Hello, world. This is nice.", "western") == "Hello, world. This is nice."  # western 保留句中标点
    assert clean_text("  Hello,   world.  ", "western") == "Hello, world."  # western 也规范化首尾/多空格(句尾 . 保留)
    assert clean_text("a\tb", "western") == "a b"  # western 制表符转空格
    assert clean_text("\"Hello\"", "western") == '"Hello"'  # ASCII 直引号不剥
    assert clean_text('"你好"') == '"你好"'  # cjk 下 ASCII 直引号同样不剥
    assert clean_text("克里斯·埃文斯说:「没问题」") == "克里斯·埃文斯说 「没问题」"
    assert clean_text("«Bonjour»", "western") == "«Bonjour»"  # 书名/引语引号不剥
    assert clean_text("好的―行") == "好的 行"  # 破折号变体 ― ‒ 也转空格
    # western 风格下残留的 CJK 全角标点应被清理(转空格),但 ASCII 标点/破折号族保留
    assert clean_text("Hello，world。", "western") == "Hello world"  # 全角逗号转空格、全角句号尾剥
    assert clean_text("Hello—world", "western") == "Hello—world"  # 破折号族保留
    assert clean_text("现在是３．５版本，时间１２：３０。", "western") == "现在是３．５版本 时间１２：３０"  # western 下全角数字仍保护
    assert clean_text("嗯。。。") == "嗯…"
    # 说话人前缀:原样透传,只清理正文
    assert clean_text("丸岡和佳奈: 本日の、ワード。") == "丸岡和佳奈: 本日の ワード"
    assert clean_text("speaker_0: Hello, world.", "western") == "speaker_0: Hello, world."
    assert clean_text("丸岡和佳奈：本日の") == "丸岡和佳奈 本日の"  # 冒号后无空格,不当作前缀
    assert clean_text("集合は12:30だよ。") == "集合は12:30だよ"  # 时间不被误判为前缀
    assert clean_text("丸岡和佳奈: 。") == ""  # 正文清空 -> 整条作废,不留 `说话人: ` 空壳
    assert _split_speaker("丸岡和佳奈: 本日の") == ("丸岡和佳奈: ", "本日の")
    assert _split_speaker("本日のワード") == ("", "本日のワード")
    # 方括号式前缀(mossland-asr 等 diarization 标签):原样保留,不被 BRACKET_MAP 改成圆括号
    assert _split_speaker("[S01] りすLOG") == ("[S01] ", "りすLOG")
    assert _split_speaker("[SPEAKER_00]  Hello") == ("[SPEAKER_00] ", "Hello")
    assert clean_text("[S01] 皆さん、こんばんは。") == "[S01] 皆さん こんばんは"
    assert clean_text("[S01] 。") == ""  # 正文清空 -> 整条作废
    # 事件描述不是说话人:非 ASCII 标签、以及方括号后无空格的,都不当前缀
    assert _split_speaker("[笑] そうですね") == ("", "[笑] そうですね")
    assert _split_speaker("[オープニングミュージック] はい") == ("", "[オープニングミュージック] はい")
    assert clean_text("[笑]真的吗?") == "(笑)真的吗?"
    assert _speaker_label("[S01] ") == "S01" and _speaker_label("関根瞳: ") == "関根瞳"
    assert _width("今天的侘寂词") == 12 and _width("Hello") == 5
    assert _ends_sentence("そうですね。") and _ends_sentence("「はい。」") and not _ends_sentence("本日の")
    # merge: 未收句 + 同说话人 + 间隔够近 -> 合并,时间轴取并集
    two = ("1\n00:00:01,000 --> 00:00:02,000\nspeaker_0: 本日の\n\n"
           "2\n00:00:02,000 --> 00:00:04,000\nspeaker_0: ワード。\n")
    merged = process(two, "merge", max_gap=2.0, max_merged_duration=20.0)
    assert [e["text"] for e in merged] == ["speaker_0: 本日のワード。"]
    assert merged[0]["start"] == 1000 and merged[0]["end"] == 4000
    # merge 的四条否决路径:已收句 / 说话人不同 / 间隔过大 / 音频事件条目
    ended = two.replace("speaker_0: 本日の\n", "speaker_0: 本日の。\n")
    assert len(process(ended, "merge", max_gap=2.0, max_merged_duration=20.0)) == 2
    other = two.replace("2\n00:00:02,000 --> 00:00:04,000\nspeaker_0:", "2\n00:00:02,000 --> 00:00:04,000\nspeaker_1:")
    assert len(process(other, "merge", max_gap=2.0, max_merged_duration=20.0)) == 2
    assert len(process(two, "merge", max_gap=0.0, max_merged_duration=20.0)) == 1  # gap 恰为 0,仍合并
    far = two.replace("00:00:02,000 --> 00:00:04,000", "00:00:09,000 --> 00:00:11,000")
    assert len(process(far, "merge", max_gap=2.0, max_merged_duration=20.0)) == 2
    assert len(process(two, "merge", max_gap=2.0, max_merged_duration=1.5)) == 2  # 合并后 3s 超上限
    event = ("1\n00:00:01,000 --> 00:00:02,000\n[オープニングミュージック]\n\n"
             "2\n00:00:02,000 --> 00:00:04,000\n本日の\n")
    assert len(process(event, "merge", max_gap=2.0, max_merged_duration=20.0)) == 2  # 音频事件不被吞并
    # merge 在方括号式前缀下同样成立:同标签合并、跨标签不合并、前缀后的事件描述不被吞并
    br_two = ("1\n00:00:01,000 --> 00:00:02,000\n[S01] 本日の\n\n"
              "2\n00:00:02,000 --> 00:00:04,000\n[S01] ワード。\n")
    assert [e["text"] for e in process(br_two, "merge", max_gap=2.0, max_merged_duration=20.0)] \
        == ["[S01] 本日のワード。"]
    assert len(process(br_two.replace("[S01] ワード。", "[S02] ワード。"), "merge",
                       max_gap=2.0, max_merged_duration=20.0)) == 2
    br_event = ("1\n00:00:01,000 --> 00:00:02,000\n[S01] [オープニングミュージック]\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\n[S01] 本日の\n")
    assert len(process(br_event, "merge", max_gap=2.0, max_merged_duration=20.0)) == 2
    # _sentences: 句末标点断句,引号内与数字里的 . 不断
    assert _sentences("はい。そうです。") == ["はい。", "そうです。"]
    assert _sentences("一文だけです。") == ["一文だけです。"]
    assert _sentences("3.5です。ね") == ["3.5です。", "ね"]
    assert _sentences("「はい。」と言った。ね") == ["「はい。」と言った。", "ね"]
    assert _sentences("本当に?!すごい") == ["本当に?!", "すごい"]
    # split: 多句粘连的超长条目按句末标点拆成一句一条(merge 的反向),时间轴按宽度比例分配
    glued = ("1\n00:00:00,000 --> 00:00:20,000\n"
             "speaker_0: 一つ目の文です。二つ目の文です。三つ目の文です。\n")
    rep = {}
    sp_parts = process(glued, "split", lang="ja", max_width=None, max_duration=12.0,
                       min_duration=1.0, report=rep)
    assert [p["text"] for p in sp_parts] == ["speaker_0: 一つ目の文です。",
                                             "speaker_0: 二つ目の文です。",
                                             "speaker_0: 三つ目の文です。"]
    assert sp_parts[0]["start"] == 0 and sp_parts[-1]["end"] == 20000  # 首尾对齐原区间
    assert all(sp_parts[i]["end"] == sp_parts[i + 1]["start"] for i in range(len(sp_parts) - 1))
    assert rep["done"] == [(1, 3)] and rep["stuck"] == []
    # 阈值内的多句条目不拆:短条目留在一起,翻译时上下文更完整
    assert len(process("1\n00:00:00,000 --> 00:00:03,000\nはい。そうです。\n", "split", lang="ja",
                       max_width=None, max_duration=12.0, min_duration=1.0, report={})) == 1
    # 宽度超限也触发拆分(即使时长没超)
    wide_glued = "1\n00:00:00,000 --> 00:00:05,000\n" + "あいうえおかきくけこ。" * 6 + "\n"
    assert len(process(wide_glued, "split", lang="ja", max_width=None, max_duration=12.0,
                       min_duration=0.0, report={})) == 6
    # 超长但内部无句读 -> 原样保留,stuck 记产物里的条目号(前面的拆分会把它顺推)
    rep2 = {}
    stuck_src = ("1\n00:00:00,000 --> 00:00:20,000\n一つ目の文です。二つ目の文です。\n\n"
                 "2\n00:00:20,000 --> 00:00:40,000\nずっと句読点がないまま話し続けている長い一文。\n")
    kept = process(stuck_src, "split", lang="ja", max_width=None, max_duration=12.0,
                   min_duration=1.0, report=rep2)
    assert len(kept) == 3 and [i for i, *_ in rep2["stuck"]] == [3]
    assert rep2["stuck"][0][3] is False  # 内部零标点 -> ASR 吃了句读,补上再切
    # 超长但内部已有标点 = 一个真正的长句(拉丁语系常见),不该被当成粘连去硬插句号
    rep3 = {}
    long_en = ("1\n00:00:00,000 --> 00:00:13,000\nOnce fallen, they have the added problem that a canal "
               "is not easy to climb out of, and being inebriated reduces motor function.\n")
    assert len(process(long_en, "split", lang="en", max_width=None, max_duration=12.0,
                       min_duration=1.0, report=rep3)) == 1
    assert rep3["stuck"][0][3] is True  # 有逗号 -> 留着,交给 resplit
    assert _has_inner_punct("ずっと句読点がないまま話し続けている長い一文。") is False
    assert _has_inner_punct("Once fallen, they have a problem.") is True
    assert _has_inner_punct("「はい、そうです。」") is True
    # stats: 只读体检,不写文件
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.srt"
        p.write_text(glued, encoding="utf-8")
        assert stats(str(p), lang="ja") == 0
    # resplit: 超宽整句切回分条,前缀每条都带且不占预算
    long_zh = "speaker_0: 今天的侘寂词 从大家投稿的侘寂词里选出想到的东西说出来 然后清爽地总结一下"
    wide = f"1\n00:00:00,000 --> 00:00:12,000\n{long_zh}\n"
    parts = process(wide, "resplit", lang="zh", min_duration=1.0, max_line_width=None)
    assert len(parts) > 1
    assert all(p["text"].startswith("speaker_0: ") for p in parts)
    assert all(_width(_split_speaker(p["text"])[1]) <= CJK_LINE_WIDTH for p in parts)
    assert all(p["end"] - p["start"] >= 1000 for p in parts)  # 无闪现
    assert parts[0]["start"] == 0 and parts[-1]["end"] == 12000  # 首尾对齐原区间
    assert all(parts[i]["end"] == parts[i + 1]["start"] for i in range(len(parts) - 1))  # 单调不重叠
    assert "".join(_split_speaker(p["text"])[1].replace(" ", "") for p in parts) == \
        _split_speaker(long_zh)[1].replace(" ", "")  # 切分不丢字
    # min-duration 抬高后段数不变(行宽是硬约束),短段从长段借时间达标
    narrow = process(wide, "resplit", lang="zh", min_duration=2.5, max_line_width=None)
    assert len(narrow) == len(parts)
    assert all(p["end"] - p["start"] >= 2500 for p in narrow)
    assert narrow[0]["start"] == 0 and narrow[-1]["end"] == 12000
    assert len(process("1\n00:00:00,000 --> 00:00:02,000\n短句\n", "resplit", lang="zh",
                       min_duration=1.0, max_line_width=None)) == 1  # 预算内原样透传
    # 数字与其后中文量词之间的排版空格不是读点,不能在那里切开(「1000 名」不该分家)
    assert _cut_cost("前 1000 名通过链接注册的人", 7) is None
    assert _cut_cost("最棒的是 这些课程免费", 4) == 0.0  # 真正的读点仍是完美切点
    # 中英混排的排版空格与真读点长得一样,但不是读点,只抬价不禁止
    assert _cut_cost("她被罚 140 欧元", 3) == MIXED_SPACE_COST      # 中文在左、数字在右仍可切
    assert _cut_cost("我也强烈推荐看看 Shima Bulgariya 的作品", 8) == MIXED_SPACE_COST
    # 拉丁专名与其黏着的中文成分不能分家,两个方向都禁
    assert _cut_cost("而 krul 的设计和它的规划", 6) is None          # 「krul 的」
    assert _cut_cost("对这个 plaskrul 挺有意见", 3) is None          # 「这个 plaskrul」
    assert _cut_cost("借鉴了最早的法式 pissoir 的设计", 8) is None   # 「法式 pissoir」
    # CJK 正文里的拉丁词内空格多半是专名内部,抬价;纯拉丁正文的词间空格照旧是完美读点
    assert _cut_cost("我也强烈推荐看看 Shima Bulgariya 的作品", 14) == LATIN_SPACE_IN_CJK_COST
    assert _cut_cost("Hello world and more", 5) == 0.0
    # 端到端:拉丁专名不该被从中文里剥出来单独成行,人名不该被词内空格劈开
    mixed = _split_body("而 krul 的设计和它的规划一样令人印象深刻", 38, zh=True)
    assert len(mixed) == 2 and min(_width(s) for s in mixed) >= 14, mixed
    name = _split_body("我也强烈推荐看看 Shima Bulgariya 的作品", 38, zh=True)
    assert any("Shima Bulgariya" in s for s in name), name
    # 中文虚词边界:「的」后优先断,但「地方」「地下」里的「地」是实词,其后不算好切点
    assert _cut_cost("独特的物体", 3, zh=True) == ZH_BREAK_AFTER_COST
    assert _cut_cost("靠近水的地方", 5, zh=True) == SCRIPT_BREAK_COST[("kanji", "kanji")]
    assert _cut_cost("红灯区是一个", 3, zh=True) == ZH_BREAK_BEFORE_COST
    assert _cut_cost("独特的物体", 3, zh=False) == SCRIPT_BREAK_COST[("kanji", "kanji")]  # 日语不受影响
    # 均分切分:40 列的句子不该被贪心切成 38+2 的孤儿尾段
    even = _split_body("阿姆斯特丹红灯区是一个臭名昭著的旅游景点", 38)  # 40 列,贪心会切成 38+2
    assert len(even) == 2 and min(_width(s) for s in even) >= 14, even
    # resplit 在方括号式前缀下同样每条都带前缀,且前缀不占行宽预算
    br_wide = f"1\n00:00:00,000 --> 00:00:12,000\n[S01] {_split_speaker(long_zh)[1]}\n"
    br_parts = process(br_wide, "resplit", lang="zh", min_duration=1.0, max_line_width=None)
    assert len(br_parts) == len(parts)
    assert all(p["text"].startswith("[S01] ") for p in br_parts)
    assert all(_width(_split_speaker(p["text"])[1]) <= CJK_LINE_WIDTH for p in br_parts)
    # split 同样透传方括号式前缀
    br_glued = ("1\n00:00:00,000 --> 00:00:20,000\n"
                "[S01] 一つ目の文です。二つ目の文です。三つ目の文です。\n")
    assert [p["text"] for p in process(br_glued, "split", lang="ja", max_width=None,
                                       max_duration=12.0, min_duration=1.0, report={})] == [
        "[S01] 一つ目の文です。", "[S01] 二つ目の文です。", "[S01] 三つ目の文です。"]
    # speakers: 列出 / --map 换真名并统一为冒号式 / --drop 去前缀
    mixed = ("1\n00:00:00,000 --> 00:00:02,000\n[S01] おはよう。\n\n"
             "2\n00:00:02,000 --> 00:00:04,000\n[S02] こんばんは。\n\n"
             "3\n00:00:04,000 --> 00:00:06,000\nナレーション。\n")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sp.srt"
        p.write_text(mixed, encoding="utf-8")
        assert speaker_counts(process(mixed, "normalize")) == [("[S01] ", 1), ("[S02] ", 1), ("", 1)]
        assert speakers(str(p)) == 0  # 只列出,不写文件
        assert p.read_text(encoding="utf-8") == mixed
        renamed = Path(d) / "renamed.srt"
        assert speakers(str(p), str(renamed), {"S01": "関根瞳", "[S02] ": "丸岡和佳奈"}) == 0
        assert [e["text"] for e in process(renamed.read_text(encoding="utf-8"), "normalize")] == [
            "関根瞳: おはよう。", "丸岡和佳奈: こんばんは。", "ナレーション。"]
        dropped = Path(d) / "dropped.srt"
        assert speakers(str(p), str(dropped), drop=True) == 0
        assert [e["text"] for e in process(dropped.read_text(encoding="utf-8"), "normalize")] == [
            "おはよう。", "こんばんは。", "ナレーション。"]
        partial = Path(d) / "partial.srt"
        speakers(str(p), str(partial), {"S01": "関根瞳"})  # 未命中的 [S02] 原样保留并 WARN
        assert "[S02] こんばんは。" in partial.read_text(encoding="utf-8")
    sample = "2\n00:00:03,500 --> 00:00:02,000\nsecond\n\n1\n00:00:01,000 --> 00:00:02,000\n第一\n行\n"
    es = process(sample, "normalize")
    assert [e["text"] for e in es] == ["第一行", "second"]
    assert es[1]["start"] == 2000 and es[1]["end"] == 3500  # 起止倒置已交换
    assert serialize(es).startswith("1\n00:00:01,000 --> 00:00:02,000\n第一行\n")
    # 端到端:-l 决定 clean 标点风格(en -> western,保留句尾句号)
    en_sample = "1\n00:00:01,000 --> 00:00:02,000\n  Hello,   world.  \n"
    en_es = process(en_sample, "clean", lang="en")
    assert [e["text"] for e in en_es] == ["Hello, world."]
    # 端到端:-l zh -> cjk(句中标点转空格、句尾句号剥)
    zh_sample = "1\n00:00:01,000 --> 00:00:02,000\n你好，世界。\n"
    zh_es = process(zh_sample, "clean", lang="zh")
    assert [e["text"] for e in zh_es] == ["你好 世界"]
    # provenance: split/resplit 插出来的点必须落在原条目区间内,真实边界一个都不能动
    with tempfile.TemporaryDirectory() as d:
        o = Path(d) / "o.srt"
        o.write_text("1\n00:00:00,000 --> 00:00:20,000\n一つ目の文です。二つ目の文です。\n", encoding="utf-8")
        sp = Path(d) / "sp.srt"
        sp.write_text(serialize(process(o.read_text(encoding="utf-8"), "split", lang="ja", max_width=None,
                                        max_duration=12.0, min_duration=1.0, report={})),
                      encoding="utf-8")
        assert provenance(str(o), str(sp)) == 0  # 拆出的中间点在 0~20s 内部
        assert provenance(str(o), str(o)) == 0  # 自己对自己:零插值
        moved = Path(d) / "moved.srt"
        moved.write_text("1\n00:00:00,000 --> 00:00:10,000\nA\n\n"
                         "2\n00:00:10,000 --> 00:00:25,000\nB\n", encoding="utf-8")
        assert provenance(str(o), str(moved)) == 1  # 25s 越出了原区间 -> 报错
    # apply: 译文只有「编号<TAB>译文」,时间轴从 base 搬,顺带跑 clean
    with tempfile.TemporaryDirectory() as d:
        b = Path(d) / "b.srt"
        b.write_text("1\n00:00:01,000 --> 00:00:02,000\n本日の\n\n"
                     "2\n00:00:02,000 --> 00:00:03,000\nワード\n", encoding="utf-8")
        tr = Path(d) / "t.txt"
        tr.write_text("1\t今天的，\n\n2\t词。\n", encoding="utf-8")
        out = Path(d) / "o.srt"
        assert apply_translation(str(b), str(tr), str(out), "zh") == 0
        assert [e["text"] for e in process(out.read_text(encoding="utf-8"), "normalize")] == ["今天的", "词"]
        assert out.read_text(encoding="utf-8").startswith("1\n00:00:01,000 --> 00:00:02,000\n今天的\n")
        # 分隔符容忍 tab / 竖线 / 冒号 / 空格,译文里以数字开头也不会被吃掉
        tr.write_text("1|今天的\n2: 3.5 个词\n", encoding="utf-8")
        assert apply_translation(str(b), str(tr), str(out), "zh") == 0
        assert [e["text"] for e in process(out.read_text(encoding="utf-8"), "normalize")] == ["今天的", "3.5 个词"]
        # 漏行 / 重号 / 越界 / 空译文 -> 报错且不写文件
        out.unlink()
        tr.write_text("1\t今天的\n", encoding="utf-8")
        assert apply_translation(str(b), str(tr), str(out), "zh") == 1 and not out.exists()
        tr.write_text("1\t今天的\n1\t重复\n2\t词\n", encoding="utf-8")
        assert apply_translation(str(b), str(tr), str(out), "zh") == 1 and not out.exists()
        tr.write_text("1\t今天的\n2\t词\n3\t多余\n", encoding="utf-8")
        assert apply_translation(str(b), str(tr), str(out), "zh") == 1 and not out.exists()
        tr.write_text("1\t今天的\n2\t。\n", encoding="utf-8")  # clean 后为空
        assert apply_translation(str(b), str(tr), str(out), "zh") == 1 and not out.exists()
    # check: 条目数/时间轴对齐
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "base.srt"
        base.write_text("1\n00:00:01,000 --> 00:00:02,000\n本日の\n\n"
                        "2\n00:00:02,000 --> 00:00:03,000\nワード\n", encoding="utf-8")
        good = Path(d) / "good.srt"
        good.write_text("1\n00:00:01,000 --> 00:00:02,000\n今天的\n\n"
                        "2\n00:00:02,000 --> 00:00:03,000\n词\n", encoding="utf-8")
        assert check(str(base), str(good)) == 0
        short = Path(d) / "short.srt"
        short.write_text("1\n00:00:01,000 --> 00:00:02,000\n今天的\n", encoding="utf-8")
        assert check(str(base), str(short)) == 1  # 条目数不一致
        shifted = Path(d) / "shifted.srt"
        shifted.write_text("1\n00:00:01,000 --> 00:00:02,000\n今天的\n\n"
                           "2\n00:00:02,500 --> 00:00:03,000\n词\n", encoding="utf-8")
        assert check(str(base), str(shifted)) == 1  # 时间轴错位
        assert check(str(base), str(shifted), fix_timeline=True) == 0  # 用 base 的时间轴覆盖并写回
        assert check(str(base), str(shifted)) == 0  # 已修好,再查就干净了
        assert [e["text"] for e in parse(shifted.read_text(encoding="utf-8"))] == ["今天的", "词"]  # 译文没被动
        assert check(str(base), str(short), fix_timeline=True) == 1  # 条目数不一致时拒绝修
        assert check(str(base), str(base)) == 0  # 全同 -> 仅 NOTE 提示漏译,不算失败
    print("self-test OK")


def main():
    # 说话人名与日文文件名要原样打得出来:Windows 上 stdout 缺省是 GBK/CP932,会把它们打成乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["init", "stats", "speakers", "merge", "split", "normalize", "clean",
                                     "apply", "resplit", "check", "provenance"],
                    nargs="?")
    ap.add_argument("input", nargs="?")
    ap.add_argument("input2", nargs="?", help="check 的第二个文件(target);apply 的译文文本")
    ap.add_argument("-o", "--output")
    ap.add_argument("-l", "--lang", help=f"语言(ISO 639-1,如 zh/ja/en)。决定 clean 的标点风格与 resplit 的行宽:cjk=逗号转空格/{CJK_LINE_WIDTH} 列,其余=保留句中标点/{LATIN_LINE_WIDTH} 列。clean/resplit 传目标语言,stats/split 作用于原文、传原文语言")
    ap.add_argument("--max-gap", type=float, default=2.0, help="merge: 超过该静音秒数就不再合并(默认 2.0)")
    ap.add_argument("--max-merged-duration", type=float, default=20.0, help="merge: 合并后单条时长上限秒数(默认 20)")
    ap.add_argument("--max-width", type=int, help=f"stats/split: 超长条目的宽度阈值(全角算 2 列)。缺省按 -l 取行宽的 {LONG_ENTRY_FACTOR} 倍,即 cjk {LONG_ENTRY_FACTOR * CJK_LINE_WIDTH} / 其余 {LONG_ENTRY_FACTOR * LATIN_LINE_WIDTH}")
    ap.add_argument("--max-duration", type=float, default=LONG_ENTRY_SEC, help=f"stats/split: 超长条目的时长阈值秒数(默认 {LONG_ENTRY_SEC:.0f})")
    ap.add_argument("--max-line-width", type=int, help=f"resplit: 每条最大显示列宽(全角算 2 列)。缺省按 -l 取 cjk {CJK_LINE_WIDTH} / 其余 {LATIN_LINE_WIDTH}")
    ap.add_argument("--min-duration", type=float, default=1.0, help="resplit: 切分出的段的最短秒数,不足则从相邻长段借时间(默认 1.0)。无需切分、原样透传的条目不受影响")
    ap.add_argument("--map", action="append", metavar="LABEL=NAME",
                    help="speakers: 把说话人标签改写为真名并统一为 `名字: `(如 --map S01=関根瞳,可重复)")
    ap.add_argument("--drop", action="store_true",
                    help="speakers: 去掉全部说话人前缀(整份只有一个说话人时用)")
    ap.add_argument("--fix-timeline", action="store_true",
                    help="check: 条目数一致时,用 base 的时间轴覆盖 target 并写回(时间轴错位一定是抄错)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.mode or not args.input:
        ap.error("mode and input are required")
    if args.mode == "init":
        return init_workspace(args.input)
    if args.mode == "stats":
        return sys.exit(stats(args.input, args.lang, args.max_width, args.max_duration))
    if args.mode == "check":
        if not args.input2:
            ap.error("check needs two files: check <base.srt> <target.srt>")
        return sys.exit(check(args.input, args.input2, args.fix_timeline))
    if args.mode == "provenance":
        if not args.input2:
            ap.error("provenance needs two files: provenance <original.srt> <derived.srt>")
        return sys.exit(provenance(args.input, args.input2))
    if args.mode == "apply":
        if not args.input2:
            ap.error("apply needs two files: apply <base.srt> <translation.txt>")
        if not args.output:
            ap.error("apply needs -o <out.srt> (it never overwrites the base)")
        return sys.exit(apply_translation(args.input, args.input2, args.output, args.lang))
    if args.mode == "speakers":
        if args.map and args.drop:
            ap.error("--map and --drop are mutually exclusive")
        mapping = {}
        for item in args.map or []:
            if "=" not in item:
                ap.error(f"--map expects LABEL=NAME, got: {item}")
            k, v = item.split("=", 1)
            if not k.strip() or not v.strip():
                ap.error(f"--map expects LABEL=NAME, got: {item}")
            mapping[k.strip()] = v.strip()
        return sys.exit(speakers(args.input, args.output, mapping, args.drop))
    report = {}
    src = process(Path(args.input).read_text(encoding="utf-8-sig"), "normalize") if args.mode == "merge" else None
    entries = process(
        Path(args.input).read_text(encoding="utf-8-sig"), args.mode, lang=args.lang,
        max_gap=args.max_gap, max_merged_duration=args.max_merged_duration,
        max_width=args.max_width, max_duration=args.max_duration,
        max_line_width=args.max_line_width, min_duration=args.min_duration, report=report,
    )
    out = args.output or args.input
    Path(out).write_text(serialize(entries), encoding="utf-8", newline="\n")
    print(f"OK: {len(entries)} entries -> {out}")  # ponytail: ASCII 输出,避开 Windows 控制台编码问题
    if args.mode == "merge":
        print(f"  merged: {len(src)} -> {len(entries)} entries")
        longest = sorted(range(len(entries)), key=lambda i: entries[i]["start"] - entries[i]["end"])[:5]
        detail = ", ".join(f"#{i + 1} ({(entries[i]['end'] - entries[i]['start']) / 1000:.1f}s)" for i in sorted(longest))
        print(f"  longest entries (spot-check these): {detail}")
    if args.mode == "split":
        done, stuck = report.get("done", []), report.get("stuck", [])
        if done:
            print(f"  split: {len(done)} long entries -> {sum(n for _, n in done)} entries")
        nopunct = [x for x in stuck if not x[3]]
        oneline = [x for x in stuck if x[3]]
        if nopunct:
            detail = ", ".join(f"#{i} ({d / 1000:.1f}s, {w}col)" for i, d, w, _ in nopunct[:10])
            print(f"  WARN {len(nopunct)} entries still over limit with no punctuation inside at all: {detail}")
            print("       the ASR dropped the sentence breaks -- add them, then run split again")
        if oneline:
            detail = ", ".join(f"#{i} ({d / 1000:.1f}s, {w}col)" for i, d, w, _ in oneline[:10])
            print(f"  NOTE {len(oneline)} entries over limit are a single, already-punctuated sentence: {detail}")
            print("       leave them alone -- `resplit` wraps them at delivery time")


if __name__ == "__main__":
    main()
