import os
import base64
import json
import re
import requests
from urllib.parse import urlparse

ORG = "socks-tjzjtm"
API_URL = f"https://api.github.com/orgs/{ORG}/repos"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
}

# leejeeooo（socks-tjzjtm 的运营者）个人账号下的节点仓库。
# 与组织仓库不同，这里是单层 base64（解一次即节点明文），
# 组织仓库是双层 base64（解两次）。decode_node_text 会自动兼容两种。
LEEJEOOOO_USER = "leejeeooo"
LEEJEOOOO_REPOS = [
    "he-izijijiliijl-",  # 81 条 vless（2026-11-07 实测）
    "dsizlijeilz",       # 2 条 vless
    "kl-iioapaal",       # 35 条 vless
    "F3fl-rpi9ol",       # 8 条 vmess
]

def _b64decode(s):
    """base64 解码，自动补 padding，失败返回空串。"""
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def decode_node_text(raw_text):
    """
    通用解码：单层或双层 base64 都能处理，返回节点明文，失败返回空串。
    逻辑：反复 base64 解码，直到结果里出现 "://"（节点 URI），
    最多 3 层。单层（leejeeooo 个人仓）解 1 次即命中，
    双层（socks-tjzjtm 组织仓）解 2 次命中。
    """
    text = (raw_text or "").strip()
    if not text:
        return ""
    for _ in range(3):
        if "://" in text:
            return text
        try:
            dec = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""
        if not dec or dec == text:
            return ""
        text = dec
    return text if "://" in text else ""

def get_latest_repos(n=200):
    """
    直接让 GitHub API 按更新时间倒序，只取前 n 个仓库，不再全量拉取再排序。
    """
    resp = requests.get(API_URL, headers=HEADERS,
                        params={"sort": "updated", "direction": "desc",
                                "per_page": n, "page": 1})
    resp.raise_for_status()
    repos = resp.json()
    names = [repo["name"] for repo in repos[:n]]
    print(f"Latest {len(names)} repos: {', '.join(names)}")
    return names

def get_repo_file_inner_text(repo, owner=ORG):
    """
    每个仓库只有一个文件，该文件内容是 base64。
    socks-tjzjtm 组织仓：双层 base64（外层 -> 单行 base64 -> 内层明文节点列表）。
    leejeeooo 个人仓：单层 base64（解一次即节点明文）。
    decode_node_text 自动兼容两种，返回节点明文。
    """
    contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    resp = requests.get(contents_url, headers=HEADERS)
    resp.raise_for_status()
    files = resp.json()

    if not isinstance(files, list) or len(files) < 1:
        print(f"  WARN: {owner}/{repo} has no files.")
        return ""

    file_info = files[0]
    outer_encoded = file_info.get("content", "")
    if not outer_encoded:
        dl = file_info.get("download_url")
        if not dl:
            print(f"  WARN: {owner}/{repo} file has neither content nor download_url.")
            return ""
        raw_text = requests.get(dl, headers=HEADERS).text
    else:
        raw_text = base64.b64decode(outer_encoded).decode("utf-8", errors="ignore")

    inner_text = decode_node_text(raw_text)
    if not inner_text:
        print(f"  WARN: {owner}/{repo} decoded empty or not a node list.")
        return ""
    return inner_text

def node_identity(node_str):
    """
    按服务器身份去重：(协议, 地址, 端口, 凭证)。
    同名不同服务器不再误删，同一服务器换个名字也不再重复。
    解析失败时返回整条节点字符串（当作唯一）。
    """
    try:
        low = node_str.lower()
        if low.startswith("vmess://"):
            payload = node_str[len("vmess://"):].split("#", 1)[0].split("?", 1)[0]
            data = json.loads(_b64decode(payload))
            return ("vmess", str(data.get("add", "")).lower(),
                    str(data.get("port", "")), str(data.get("id", "")))
        if low.startswith("ss://"):
            rest = node_str[len("ss://"):].split("#", 1)[0]
            if "@" in rest:
                userinfo, _, hostport = rest.rpartition("@")
                raw = _b64decode(userinfo)
                pwd = raw.split(":", 1)[-1] if ":" in raw else raw
                host, _, port = hostport.rpartition(":")
                return ("ss", host.lower(), port, pwd)
            raw = _b64decode(rest)
            if "@" in raw:
                cred, _, hp = raw.rpartition("@")
                host, _, port = hp.rpartition(":")
                pwd = cred.split(":", 1)[-1] if ":" in cred else cred
                return ("ss", host.lower(), port, pwd)
            return node_str
        parsed = urlparse(node_str)
        proto = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = str(parsed.port or "")
        if proto == "vless":
            return ("vless", host, port, parsed.username or "")
        if proto == "trojan":
            # trojan 密码在 @ 前面的 username 位置
            return ("trojan", host, port, parsed.username or "")
        return node_str
    except Exception:
        return node_str

def collect_from_repos(repos, owner, all_nodes, seen):
    """从一批仓库收集节点并去重，返回新增数。"""
    added_total = 0
    for repo in repos:
        print(f"Processing repository: {owner}/{repo}")
        inner_text = get_repo_file_inner_text(repo, owner=owner)
        if not inner_text:
            continue

        lines = [ln.strip() for ln in inner_text.splitlines() if ln.strip()]
        print(f"  Decoded {len(lines)} inner lines from {owner}/{repo}")

        added = 0
        for node in lines:
            key = node_identity(node)
            if key not in seen:
                seen.add(key)
                all_nodes.append(node)
                added += 1
        print(f"  Unique nodes added from {owner}/{repo}: {added}")
        added_total += added
    return added_total

def main():
    all_nodes = []
    seen = set()

    # 源 1：socks-tjzjtm 组织（双层 base64），取最新 200 个仓
    # 2026-11-07 用户令放宽到 200：最新 60 仓是同一快照（组织部分仅 66 条），
    # 取 200 可拿到 3–4 代快照，去重后组织部分约 150–200 条；fast-top 仍只留最快前 100 条。
    repos = get_latest_repos(200)  # 这里可以改成任意数量
    collect_from_repos(repos, ORG, all_nodes, seen)

    # 源 2：leejeeooo 个人仓库（单层 base64，同运营者、同一节点池）
    collect_from_repos(LEEJEOOOO_REPOS, LEEJEOOOO_USER, all_nodes, seen)

    print(f"Total unique nodes collected: {len(all_nodes)}")

    with open("fast.txt", "w", encoding="utf-8") as f:
        for node in all_nodes:
            f.write(node + "\n")

    print("fast.txt updated successfully.")

    # fast-top.txt 由 E2 真实链路测速生成（见 fast.yml），此处只写完整合并列表

if __name__ == "__main__":
    main()
