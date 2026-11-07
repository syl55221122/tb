import asyncio
import re
import time
from typing import List, Tuple, Optional
import socket
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

INPUT_FILE = 'v2.txt'
OUTPUT_FILE = 'v2-top30.txt'  # 测速精选版单独输出，不覆盖 v2.txt 的完整合并列表
TOP_NODES = 30
CONCURRENCY = 200

# 提取协议和域名/IP
NODE_REGEX = re.compile(r'^(?P<protocol>vless|vmess|trojan|ss)://(?P<rest>.+)', re.IGNORECASE)
HOST_REGEX = re.compile(r'@(?P<host>[^:]+)')

def extract_info(line: str) -> Tuple[Optional[str], Optional[str]]:
    match = NODE_REGEX.match(line)
    if not match:
        return None, None
    protocol = match.group('protocol').upper()
    rest = match.group('rest')
    host_match = HOST_REGEX.search(rest)
    host = host_match.group('host') if host_match else None
    return protocol, host

def get_ip_country(ip: str) -> str:
    COUNTRY_CODES = {
        'US': '美国', 'CN': '中国', 'JP': '日本', 'SG': '新加坡', 'KR': '韩国',
        'GB': '英国', 'FR': '法国', 'DE': '德国', 'AU': '澳大利亚', 'CA': '加拿大',
        'HK': '中国香港', 'TW': '中国台湾', 'IN': '印度', 'RU': '俄罗斯', 'BR': '巴西',
        'MX': '墨西哥', 'NL': '荷兰', 'SE': '瑞典', 'CH': '瑞士', 'IT': '意大利',
        'ES': '西班牙', 'Unknown': '未知'
    }
    try:
        socket.inet_aton(ip)
        session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        try:
            r = session.get(f'https://ipwhois.app/json/{ip}', timeout=10)
            if r.status_code == 200:
                data = r.json()
                country = data.get('country', '')
                if country == 'United States': return '美国'
                elif country == 'Japan': return '日本'
                elif country == 'Singapore': return '新加坡'
                elif country == 'South Korea': return '韩国'
                elif country == 'China': return '中国'
                elif country == 'Hong Kong': return '中国香港'
                elif country == 'Taiwan': return '中国台湾'
                elif len(country) == 2: return COUNTRY_CODES.get(country, country)
                return country
        except: pass

        try:
            r = session.get(f'http://ip-api.com/json/{ip}?fields=countryCode', timeout=10)
            if r.status_code == 200:
                data = r.json()
                code = data.get('countryCode', '')
                return COUNTRY_CODES.get(code, code)
        except: pass

        # fallback for known Cloudflare IPs
        octets = ip.split('.')
        if octets[:2] in [['104', '18'], ['108', '162'], ['162', '159'], ['172', '64']]:
            return '美国'
    except: pass
    return '未知'

async def measure_connect_latency_ms(domain: str, port: int = 443, timeout: float = 1, attempts: int = 2) -> Optional[float]:
    best_ms = None
    for _ in range(attempts):
        start = time.perf_counter()
        try:
            connect_coro = asyncio.open_connection(domain, port, ssl=False)
            reader, writer = await asyncio.wait_for(connect_coro, timeout=timeout)
            writer.close()
            try: await writer.wait_closed()
            except: pass
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            best_ms = elapsed_ms if best_ms is None else min(best_ms, elapsed_ms)
        except: pass
    return best_ms

async def gather_latencies(nodes: List[Tuple[str, str, str]]) -> List[Tuple[str, Optional[float], str]]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = []

    async def probe(line: str, protocol: str, host: str):
        async with semaphore:
            latency = await measure_connect_latency_ms(host)
            location = get_ip_country(host)
            results.append((line, latency, location))

    tasks = [probe(line, protocol, host) for line, protocol, host in nodes]
    await asyncio.gather(*tasks)
    return results

def load_nodes() -> List[Tuple[str, str, str]]:
    nodes = []
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            protocol, host = extract_info(line)
            if host:
                nodes.append((line, protocol, host))
    return nodes

def save_top_nodes(results: List[Tuple[str, Optional[float], str]]):
    sorted_results = sorted(results, key=lambda x: x[1] if x[1] is not None else float('inf'))
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for idx, (line, latency, location) in enumerate(sorted_results[:TOP_NODES], 1):
            tag = f"{int(round(latency))}ms" if latency is not None else "timeout"
            new_name = f"{idx:02d}_{location}_{tag}"
            if '#' in line:
                line = re.sub(r'#.*$', f'#{new_name}', line)
            else:
                line += f'#{new_name}'
            f.write(line + '\n')

async def main():
    nodes = load_nodes()
    results = await gather_latencies(nodes)
    save_top_nodes(results)

if __name__ == '__main__':
    asyncio.run(main())
