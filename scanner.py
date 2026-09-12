import hashlib
import json
import os
import re
from pathlib import Path
from statistics import median
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE = Path("state.json")
CITIES = ("yalova", "kocaeli")


def number(text):
    cleaned = re.sub(r"[^0-9,.-]", "", text or "").replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def load_sources():
    raw = os.getenv("SOURCE_CONFIG_JSON", "[]")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def scan_source(source):
    response = requests.get(source["url"], headers={"User-Agent": "ArsaFirsatRobotu/1.0"}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    nodes = soup.select(source.get("item", "")) if source.get("item") else []
    if not nodes:
        nodes = [a.parent for a in soup.select("a[href]") if any(
            word in a.parent.get_text(" ", strip=True).casefold()
            for word in ("arsa", "tarla", "arazi", "bahçe")
        )]
    for node in nodes:
        def text(selector):
            found = node.select_one(source.get(selector, ""))
            return found.get_text(" ", strip=True) if found else ""
        title = text("title") or node.get_text(" ", strip=True)[:160]
        location = text("location") or node.get_text(" ", strip=True)
        if not any(city in location.casefold() for city in CITIES):
            continue
        raw = node.get_text(" ", strip=True)
        price = number(text("price"))
        area = number(text("area"))
        if not price:
            match = re.search(r"([\d.]+(?:,\d+)?)\s*(?:TL|₺)", raw, re.I)
            price = number(match.group(1)) if match else 0
        if not area:
            match = re.search(r"([\d.]+(?:,\d+)?)\s*(?:m²|m2)", raw, re.I)
            area = number(match.group(1)) if match else 0
        link_node = node.select_one(source.get("link", "a"))
        link = urljoin(source["url"], link_node.get("href", "")) if link_node else source["url"]
        listing_id = hashlib.sha256(f"{source['name']}|{link}".encode()).hexdigest()[:20]
        if price > 0 and area > 0:
            rows.append({"id": listing_id, "source": source["name"], "category": source.get("category", "diğer"),
                         "title": title, "location": location, "price": price, "area": area,
                         "m2": price / area, "link": link})
    return rows


def district(location):
    parts = [p.strip().casefold() for p in re.split(r"[/,>-]", location) if p.strip()]
    return " / ".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "bilinmiyor")


def enrich_comparables(rows):
    groups = {}
    for row in rows:
        groups.setdefault(district(row["location"]), []).append(row["m2"])
    for row in rows:
        values = groups[district(row["location"])]
        benchmark = median(values)
        row["emsal_m2"] = benchmark
        row["emsale_gore"] = (row["m2"] / benchmark - 1) * 100 if benchmark else 0
    return rows


def send(message):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram secrets eksik")
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True}, timeout=20)
    response.raise_for_status()


def main():
    sources = load_sources()
    if not sources:
        print("SOURCE_CONFIG_JSON boş; kaynak yapılandırması bekleniyor")
        return
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    rows = []
    for source in sources:
        try:
            rows.extend(scan_source(source))
        except Exception as exc:
            print(f"{source.get('name')}: {type(exc).__name__}")
    enrich_comparables(rows)
    current = {}
    for row in rows:
        old = previous.get(row["id"])
        reason = None
        if old and row["price"] < old.get("price", row["price"]):
            reason = f"📉 FİYAT DÜŞTÜ: {old['price']:,.0f} TL → {row['price']:,.0f} TL"
        elif not old:
            reason = "🆕 YENİ ARSA İLANI"
        if row["emsale_gore"] <= -15:
            reason = f"🔥 FIRSAT: emsalden %{abs(row['emsale_gore']):.0f} ucuz" + (f"\n{reason}" if reason else "")
        if reason:
            send(f"{reason}\n{row['title']}\n{row['location']}\n{row['area']:,.0f} m² | {row['price']:,.0f} TL | {row['m2']:,.0f} TL/m²\nKaynak: {row['source']} ({row['category']})\n{row['link']}\n⚠️ İlan doğrulanmalıdır; yatırım tavsiyesi değildir.")
        current[row["id"]] = {"price": row["price"], "link": row["link"]}
    STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
