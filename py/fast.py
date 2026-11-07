import asyncio
import os
import base64
import json
import re
import requests
from urllib.parse import urlparse

# 复用 step2 的测速与归属地查询（同一目录下）
from step2_test_v2_nodes import measure_connect_latency_ms, get_ip_country

ORG = "socks-tjzjtm"
API_URL = f"https://api.github.com/orgs/{ORG}/repos"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
}
TOP_NODES = 100
CONCURRENCY = 200

def _b64decode(s):
    """base64 解码，自动补 padding，失败返回空串。"""
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def get_latest_repos(n=60):
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

def get_repo_file_inner_text(repo):
    """
    每个仓库只有一个文件，该文件内容是 base64（外层）。
    文件内只有一行，这一行又是 base64（内层，订阅内容）。
    需要两次解码：外层 -> 单行 base64 -> 内层明文节点列表。
    """
    contents_url = f"https://api.github.com/repos/{ORG}/{repo}/contents"
    resp = requests.get(contents_url, headers=HEADERS)
    resp.raise_for_status()
    files = resp.json()

    if not isinstance(files, list) or len(files) < 1:
        print(f"  WARN: {repo} has no files.")
        return ""

    file_info = files[0]
    outer_encoded = file_info.get("content", "")
    if not outer_encoded:
        dl = file_info.get("download_url")
        if not dl:
            print(f"  WARN: {repo} file has neither content nor download_url.")
            return ""
        outer_text = requests.get(dl, headers=HEADERS).text
    else:
        outer_text = base64.b64decode(outer_encoded).decode("utf-8", errors="ignore")

    inner_line = outer_text.strip()
    if not inner_line:
        print(f"  WARN: {repo} outer decoded is empty.")
        return ""

    try:
        inner_text = base64.b64decode(inner_line).decode("utf-8", errors="ignore")
        return inner_text
    except Exception as e:
        print(f"  ERROR: {repo} inner base64 decode failed: {e}")
        return ""

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

def extract_host_port(node_str):
    """从节点链接里提取 (host, port)，供测速用；解析失败返回 (None, None)。"""
    try:
        low = node_str.lower()
        if low.startswith("vmess://"):
            payload = node_str[len("vmess://"):].split("#", 1)[0].split("?", 1)[0]
            data = json.loads(_b64decode(payload))
            return str(data.get("add", "")), int(data.get("port") or 443)
        if low.startswith("ss://"):
            rest = node_str[len("ss://"):].split("#", 1)[0]
            if "@" in rest:
                _, _, hostport = rest.rpartition("@")
                host, _, port = hostport.rpartition(":")
                return host, int(port or 443)
            raw = _b64decode(rest)
            if "@" in raw:
                _, _, hp = raw.rpartition("@")
                host, _, port = hp.rpartition(":")
                return host, int(port or 443)
            return None, None
        parsed = urlparse(node_str)
        host = parsed.hostname
        if not host:
            return None, None
        return host, int(parsed.port or 443)
    except Exception:
        return None, None

async def test_and_write_top(nodes):
    """测速并把最快 TOP_NODES 条写进 fast-top.txt（带国家与延迟重命名）。"""
    sem = asyncio.Semaphore(CONCURRENCY)
    results = []

    async def probe(line):
        async with sem:
            host, port = extract_host_port(line)
            if not host:
                results.append((line, None, "未知"))
                return
            latency = await measure_connect_latency_ms(host, port=port)
            location = get_ip_country(host)
            results.append((line, latency, location))

    await asyncio.gather(*[probe(n) for n in nodes])
    results.sort(key=lambda x: x[1] if x[1] is not None else float("inf"))
    with open("fast-top.txt", "w", encoding="utf-8") as f:
        for idx, (line, latency, location) in enumerate(results[:TOP_NODES], 1):
            tag = f"{int(round(latency))}ms" if latency is not None else "timeout"
            new_name = f"{idx:02d}_{location}_{tag}"
            if "#" in line:
                line = re.sub(r"#.*$", f"#{new_name}", line)
            else:
                line += f"#{new_name}"
            f.write(line + "\n")
    print(f"fast-top.txt updated: {min(len(results), TOP_NODES)} nodes.")

def main():
    repos = get_latest_repos(60)  # 这里可以改成任意数量
    all_nodes = []
    seen = set()

    for repo in repos:
        print(f"Processing repository: {repo}")
        inner_text = get_repo_file_inner_text(repo)
        if not inner_text:
            continue

        lines = [ln.strip() for ln in inner_text.splitlines() if ln.strip()]
        print(f"  Decoded {len(lines)} inner lines from {repo}")

        added = 0
        for node in lines:
            key = node_identity(node)
            if key not in seen:
                seen.add(key)
                all_nodes.append(node)
                added += 1
        print(f"  Unique nodes added from {repo}: {added}")

    print(f"Total unique nodes collected: {len(all_nodes)}")

    with open("fast.txt", "w", encoding="utf-8") as f:
        for node in all_nodes:
            f.write(node + "\n")

    print("fast.txt updated successfully.")

    # 测速精选版单独输出，不覆盖 fast.txt 的完整合并列表
    if all_nodes:
        asyncio.run(test_and_write_top(all_nodes))

if __name__ == "__main__":
    main()
