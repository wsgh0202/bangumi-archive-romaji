#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set


ALIAS_BLOCK_RE = re.compile(r"(\|别名=\{)(.*?)(\})", re.DOTALL)


def normalize(text: str) -> str:
    return text.strip().casefold()


def parse_infobox_aliases(infobox: str) -> List[str]:
    """从 infobox 的 `|别名={...}` 区块中提取别名列表。"""
    m = ALIAS_BLOCK_RE.search(infobox)
    if not m:
        return []

    body = m.group(2).strip("\r\n")
    aliases: List[str] = []
    for raw_line in re.split(r"\r?\n", body):
        line = raw_line.strip()
        if not line:
            continue

        # 别名格式是 [别名]，需要去掉方括号。
        if line.startswith("[") and line.endswith("]") and len(line) >= 2:
            candidate = line[1:-1].strip()
            if candidate:
                aliases.append(candidate)

    return aliases


def replace_infobox_aliases(infobox: str, aliases: List[str]) -> str:
    """替换 `|别名={...}` 内的别名。"""
    # 读取原 infobox 中的换行格式，保持一致性。
    newline = "\r\n" if "\r\n" in infobox else "\n"
    # 保持别名格式为 [别名]
    alias_lines = [f"[{a}]" for a in aliases]
    # 拼接成新的别名区块内容
    new_body = newline.join(alias_lines)

    # 若已存在别名区块，则替换其中内容，保持区块前后其他内容不变。
    m = ALIAS_BLOCK_RE.search(infobox)
    if m:
        replacement = f"{m.group(1)}{newline}{new_body}{newline}{m.group(3)}"
        return infobox[: m.start()] + replacement + infobox[m.end() :]

    return infobox


def replace_infobox_aliases_raw(raw_text: str, aliases: List[str]) -> str:
    """在 JSONL 原始行中，仅替换命中的 |别名={...} 区块。"""
    m = ALIAS_BLOCK_RE.search(raw_text)
    if not m:
        return raw_text

    newline = "\\r\\n" if "\\r\\n" in m.group(2) else "\\n"
    # 仅对新增的别名行做 JSON 字符串级转义，避免破坏原有其他内容。
    alias_lines = [json.dumps(f"[{a}]", ensure_ascii=False)[1:-1] for a in aliases]
    new_body = newline.join(alias_lines)
    replacement = f"{m.group(1)}{newline}{new_body}{newline}{m.group(3)}"
    return raw_text[: m.start()] + replacement + raw_text[m.end() :]


def read_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def build_anime_name_index(anime_rows: List[Dict]) -> Dict[str, List[str]]:
    """
    构建索引：规范化名称 -> 对应条目的全部名称（title + synonyms）。
    """
    index: Dict[str, List[str]] = {}

    for row in anime_rows:
        names: List[str] = []
        # 从 title 字段收集名称
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            names.append(title.strip())

        # 从 synonyms 字段收集所有名称
        synonyms = row.get("synonyms")
        if isinstance(synonyms, list):
            for s in synonyms:
                if isinstance(s, str) and s.strip():
                    names.append(s.strip())

        # 先在单条 anime 记录内部去重，避免同义名重复污染索引。
        unique_names: List[str] = []
        seen_local: Set[str] = set()
        for n in names:
            k = normalize(n)
            if k and k not in seen_local:
                seen_local.add(k)
                unique_names.append(n)

        if not unique_names:
            continue

        def append_unique(target: List[str], values: List[str]) -> None:
            # 在保留原有顺序的前提下，追加不重复名称。
            seen = {normalize(n) for n in target}
            for n in values:
                key = normalize(n)
                if key not in seen:
                    target.append(n)
                    seen.add(key)

        # 对于该条记录中的每个名字，都指向同一组去重后的名字列表，
        # 这样后续按任意别名命中时，都能拿到完整候选集合。
        for n in unique_names:
            key = normalize(n)
            if key not in index:
                index[key] = []
            append_unique(index[key], unique_names)

    return index


def merge_aliases_stream(
    anime_rows: List[Dict], bangumi_path: Path, output_path: Path
) -> int:
    """逐条读取 Bangumi JSONL，并将合并结果逐条写入输出文件。

    Returns:
        int: 写出的记录条数
    """
    anime_index = build_anime_name_index(anime_rows)
    written = 0

    with (
        bangumi_path.open("r", encoding="utf-8") as src,
        output_path.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            line = line.strip()
            if not line:
                continue

            subject = json.loads(line)

            # 仅处理 type=2（动画）条目
            type = subject.get("type") if isinstance(subject.get("type"), int) else 0
            if type != 2:
                dst.write(line)
                dst.write("\n")
                written += 1
                continue

            # 读取名称相关字段
            name = subject.get("name") if isinstance(subject.get("name"), str) else ""
            name_cn = (
                subject.get("name_cn")
                if isinstance(subject.get("name_cn"), str)
                else ""
            )
            infobox = (
                subject.get("infobox")
                if isinstance(subject.get("infobox"), str)
                else ""
            )
            existing_aliases = parse_infobox_aliases(infobox)

            # 收集已存在名字集合（name/name_cn/现有别名），用于避免回填重复内容。
            existing_norm: Set[str] = set()
            for v in [name, name_cn, *existing_aliases]:
                if v.strip():
                    existing_norm.add(normalize(v))

            # 只要 Bangumi 的 name/name_cn/别名中任一项命中 anime 索引，即纳入候选。
            matched_name_sets: List[str] = []
            for key in existing_norm:
                if key in anime_index:
                    matched_name_sets = anime_index[key]
                    break

            # 保持别名顺序稳定：先保留原有别名，再追加新合并名称。
            merged_aliases: List[str] = list(existing_aliases)
            merged_norm_aliases: Set[str] = {normalize(a) for a in existing_aliases}

            # 追加“不在原字段里”的名称。
            for candidate in matched_name_sets:
                ckey = normalize(candidate)
                if not ckey:
                    continue
                if ckey in existing_norm or ckey in merged_norm_aliases:
                    continue
                merged_aliases.append(candidate)
                merged_norm_aliases.add(ckey)

            # 如果别名没有变化，直接写回原行，保证完全不改动原始内容。
            if merged_aliases == existing_aliases:
                dst.write(line)
                dst.write("\n")
                written += 1
                continue

            # 每行最多一个 |别名 区块，直接替换该命中片段即可，其他内容保持原样。
            new_line = replace_infobox_aliases_raw(line, merged_aliases)

            dst.write(new_line)
            dst.write("\n")
            written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "当 Bangumi 的 name/name_cn/别名中存在大小写不敏感的完全匹配时，"
            "将 anime-offline JSONL 里的 title/synonyms 合并到 Bangumi infobox 别名。"
        )
    )
    parser.add_argument(
        "--anime",
        default="anime-offline-database.jsonl",
        help="anime-offline JSONL 路径",
    )
    parser.add_argument("--bangumi", default="bangumi.jsonl", help="Bangumi JSONL 路径")
    parser.add_argument(
        "--output", default="bangumi.alias_merged.jsonl", help="输出 JSONL 路径"
    )
    args = parser.parse_args()

    anime_rows = read_jsonl(Path(args.anime))
    written = merge_aliases_stream(anime_rows, Path(args.bangumi), Path(args.output))

    print(f"完成：已写入 {written} 条记录到 {args.output}")


if __name__ == "__main__":
    main()
