#!/usr/bin/env python3
import argparse
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path


BANGUMI_LATEST_URL = "https://raw.githubusercontent.com/bangumi/Archive/refs/heads/master/aux/latest.json"


def fetch_json(url: str) -> dict:
    """拉取远程 JSON 元数据。"""
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)


def load_json(path: Path) -> dict:
    """读取本地版本文件，不存在或损坏时返回空字典。"""
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_json(path: Path, payload: dict) -> None:
    """保存版本信息到本地 JSON 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def download_file(url: str, output_path: Path) -> None:
    """下载文件到目标路径。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, output_path.open("wb") as dst:
        shutil.copyfileobj(resp, dst)


def resolve_bangumi_archive_state(version_path: Path) -> tuple[dict, bool]:
    """返回远端元数据，以及其相对本地版本记录是否发生变化。"""
    latest = fetch_json(BANGUMI_LATEST_URL)
    latest_digest = latest.get("digest", "")
    if not latest_digest:
        raise RuntimeError("latest.json 中缺少 digest 字段。")

    current = load_json(version_path)
    current_digest = current.get("digest", "")
    return latest, current_digest != latest_digest


def check_bangumi_archive_changed(version_file: str = "version/bangumi.json") -> bool:
    """检查 Bangumi 数据源相对本地版本记录是否发生变化。"""
    _, changed = resolve_bangumi_archive_state(Path(version_file))
    return changed


def update_bangumi_archive(
    version_file: str = "version/bangumi.json",
    download_dir: str = "downloads",
    extract_dir: str = "build/bangumi_archive",
    force_download: bool = False,
) -> bool:
    """执行 Bangumi 数据更新，并返回数据源是否发生变化。"""
    version_path = Path(version_file)
    download_dir_path = Path(download_dir)
    extract_dir_path = Path(extract_dir)

    # 1) 拉取远端元数据，并与本地 digest 判断数据源是否变化。
    latest, has_new_version = resolve_bangumi_archive_state(version_path)
    changed = force_download or has_new_version

    if force_download:
        print("已启用强制下载，跳过本地版本记录比对。")

    # 2) 若版本未变化且未强制下载，则直接结束，不处理本地构建输入。
    if not force_download and not changed:
        print("Bangumi 数据已是最新（digest 未变化）。")
        return changed

    # 3) 下载并重新解压当前版本，最后回写版本元数据。
    download_url = latest.get("browser_download_url")
    name = latest.get("name")
    if not download_url or not name:
        raise RuntimeError("latest.json 缺少 browser_download_url 或 name。")

    zip_path = download_dir_path / name
    print(f"开始下载：{download_url}")
    download_file(download_url, zip_path)

    if extract_dir_path.exists():
        shutil.rmtree(extract_dir_path)
    extract_dir_path.mkdir(parents=True, exist_ok=True)
    print(f"开始解压到：{extract_dir_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir_path)

    save_json(version_path, latest)
    print(f"版本元数据已更新：{version_path}")
    return changed


def main() -> int:
    """执行 Bangumi 数据更新流程。"""
    parser = argparse.ArgumentParser(
        description="检查 Bangumi Archive 最新元数据，digest 变化时下载并解压。"
    )
    parser.add_argument(
        "--version-file",
        default="version/bangumi.json",
        help="保存 Bangumi 最新元数据的 JSON 文件路径。",
    )
    parser.add_argument(
        "--download-dir",
        default="downloads",
        help="下载 zip 文件的保存目录。",
    )
    parser.add_argument(
        "--extract-dir",
        default="build/bangumi_archive",
        help="zip 解压目标目录。",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="忽略本地版本文件中的 digest 记录，直接下载并重新解压。",
    )
    args = parser.parse_args()

    try:
        update_bangumi_archive(
            version_file=args.version_file,
            download_dir=args.download_dir,
            extract_dir=args.extract_dir,
            force_download=args.force_download,
        )
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
