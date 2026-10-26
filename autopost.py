"""Автопостинг каруселей из t.me/adresakzn в Instagram @keyby_kzn.

Команды:
  python autopost.py prepare            — выбрать объявление, собрать слайды в posts/<id>/
  python autopost.py publish <id>       — опубликовать карусель posts/<id>/ в Instagram
  python autopost.py refresh            — продлить токен Instagram (печатает новый токен)
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
STATE = ROOT / "state" / "posted.json"
FONTS = ROOT / "fonts"

CHANNEL = os.environ.get("TG_CHANNEL", "adresakzn")
REPO = os.environ.get("GITHUB_REPOSITORY", "mkrvu/keyby_kzn-autopost")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
GRAPH = "https://graph.instagram.com/v23.0"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

W, H = 1080, 1350
BG, INK, GREY, LINE = (241, 241, 238), (38, 38, 38), (122, 122, 118), (211, 211, 207)
ACCENT = (116, 184, 81)  # зелёный кейби
WHITE = (255, 255, 255)
LOGO = "КЕЙБИ.РФ"


# ---------------------------------------------------------------- Telegram

def fetch_page(before=None):
    url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_messages(soup):
    out = []
    for m in soup.select(".tgme_widget_message"):
        post = m.get("data-post", "")
        if "/" not in post:
            continue
        mid = int(post.split("/")[1])
        photos = []
        for a in m.select("a.tgme_widget_message_photo_wrap"):
            mm = re.search(r"background-image:url\('([^']+)'\)", a.get("style", ""))
            if mm:
                photos.append(mm.group(1))
        t = m.select_one(".tgme_widget_message_text")
        text = t.get_text("\n") if t else ""
        link = ""
        for b in m.select("a.tgme_widget_message_inline_button, a"):
            href = b.get("href", "")
            if "keyby.ru/apartments/" in href:
                link = href.split("?")[0]
                break
        out.append({"id": mid, "photos": photos, "text": text, "link": link})
    return out


def collect_listings(pages=3):
    msgs, before = [], None
    for _ in range(pages):
        soup = fetch_page(before)
        page = parse_messages(soup)
        if not page:
            break
        msgs = page + msgs
        before = min(m["id"] for m in page)
    msgs.sort(key=lambda m: m["id"])
    by_id = {m["id"]: m for m in msgs}
    listings = []
    for i, m in enumerate(msgs):
        if len(m["photos"]) < 5:
            continue
        # текст объявления идёт отдельным сообщением сразу после альбома
        txt = m if "Номер объекта" in m["text"] else by_id.get(m["id"] + len(m["photos"]))
        if not txt or "Номер объекта" not in txt["text"]:
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            txt = nxt if nxt and "Номер объекта" in nxt["text"] else None
        if not txt:
            continue
        info = parse_text(txt["text"])
        if not info:
            continue
        info.update(photos=m["photos"][:10], link=txt["link"], tg=f"https://t.me/{CHANNEL}/{m['id']}")
        listings.append(info)
    return listings


TYPES = [
    (r"студия", "Студия", "СТУДИЯ", "СТУДИЯ"),
    (r"евро-?2\s*к", "Евро-2к", "ЕВРО-ДВУШКА", "ЕВРО-2К КВАРТИРА"),
    (r"евро-?3\s*к", "Евро-3к", "ЕВРО-ТРЁШКА", "ЕВРО-3К КВАРТИРА"),
    (r"евро-?4\s*к", "Евро-4к", "ЕВРО-4К", "ЕВРО-4К КВАРТИРА"),
    (r"1\s*к", "1к", "ОДНУШКА", "1-К КВАРТИРА"),
    (r"2\s*к", "2к", "ДВУШКА", "2-К КВАРТИРА"),
    (r"3\s*к", "3к", "ТРЁШКА", "3-К КВАРТИРА"),
    (r"4\s*к", "4к", "4-КОМНАТНАЯ", "4-К КВАРТИРА"),
    (r"комната", "Комната", "КОМНАТА", "КОМНАТА"),
]


def money(s):
    d = re.sub(r"\D", "", s or "")
    return f"{int(d):,}".replace(",", ".") if d else ""


def parse_text(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    head = lines[0]
    t = None
    for rx, short, cover, title in TYPES:
        mm = re.match(rf"^({rx})\s*(.*)$", head, re.I)
        if mm:
            t = (short, cover, title, mm.group(mm.lastindex).strip())
            break
    if not t:
        return None
    info = {"type": t[0], "cover": t[1], "title": t[2], "name": t[3] or ""}
    body = "\n".join(lines)
    pm = re.search(r"^([\d\s.]{4,})\s*(\+\s*ку)?", "\n".join(lines[1:4]), re.I | re.M)
    if not pm:
        return None
    info["price"] = money(pm.group(1))
    info["ku"] = bool(pm.group(2))
    for key, rx in [("deposit", r"Залог[:\s]*([^\n]+)"), ("commission", r"Комиссия[:\s]*([^\n]+)"),
                    ("floor", r"Этаж[:\s]*([^\n]+)"), ("area", r"Площадь[:\s]*([\d.,]+)"),
                    ("object", r"Номер объекта[:\s]*(\d+)")]:
        mm = re.search(rx, body, re.I)
        info[key] = mm.group(1).strip() if mm else ""
    if not info["object"]:
        return None
    if re.search(r"без\s+залога", body, re.I):
        info["deposit"] = "без залога"
    elif info["deposit"]:
        info["deposit"] = money(info["deposit"]) + "₽" if re.search(r"\d", info["deposit"]) else info["deposit"]
    info["pets"] = "можно" if re.search(r"можно с животн", body, re.I) else ("нельзя" if re.search(r"без животн|нельзя с животн", body, re.I) else "")
    addr = lines[1] if len(lines) > 1 and not re.match(r"^[\d\s.]{4,}", lines[1]) else ""
    info["address"] = addr if addr.lower() != info["name"].lower() else ""
    district = ""
    for tag in re.findall(r"#([^\s@#]+)", body):
        if not re.match(r"^(евро)?\d*к$|^студия$|^комната$", tag, re.I):
            district = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", "-", tag)
            break
    info["district"] = district
    return info


# ---------------------------------------------------------------- рендер

def font(name, size):
    return ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)


def load_img(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def cover_fit(d_img, im, x, y, w, h):
    w, h = int(w), int(h)
    s = max(w / im.width, h / im.height)
    nw, nh = max(w, round(im.width * s)), max(h, round(im.height * s))
    r = im.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    d_img.paste(r.crop((l, t, l + w, t + h)), (int(x), int(y)))


def fit_font(draw, text, max_w, start, name="Unbounded-Black", min_size=40):
    s = start
    while s > min_size:
        f = font(name, s)
        if draw.textlength(text, font=f) <= max_w:
            return f
        s -= 2
    return font(name, min_size)


def pill(draw, x, y, h, text, size, bg=ACCENT, fg=WHITE):
    f = font("Manrope-Bold", size)
    w = draw.textlength(text, font=f) + h * 0.9
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=bg)
    draw.text((x + h * 0.45, y + h / 2), text, font=f, fill=fg, anchor="lm")
    return w


def arrow(draw, cx, cy, half, color=WHITE, width=4):
    draw.line((cx - half, cy, cx + half, cy), fill=color, width=width)
    draw.line((cx + half - 14, cy - 12, cx + half, cy), fill=color, width=width)
    draw.line((cx + half - 14, cy + 12, cx + half, cy), fill=color, width=width)


def price_line(info):
    return f"{info['price']}₽" + (" + КУ" if info["ku"] else "")


def slide_cover(info, ims):
    img = Image.new("RGB", (W, H), (17, 17, 17))
    g = 6
    cover_fit(img, ims[0], 0, 0, W / 2 - g / 2, 560)
    cover_fit(img, ims[1 % len(ims)], W / 2 + g / 2, 0, W / 2 - g / 2, 700)
    cover_fit(img, ims[2 % len(ims)], 0, 560 + g, W / 2 - g / 2, H - 560 - g)
    cover_fit(img, ims[3 % len(ims)], W / 2 + g / 2, 700 + g, W / 2 - g / 2, H - 700 - g)
    # градиент снизу
    grad = Image.new("L", (1, H))
    for yy in range(H):
        t = max(0.0, (yy - H * 0.35) / (H * 0.65))
        a = 0.6 * min(1, t / 0.45) if t < 0.45 else 0.6 + 0.28 * (t - 0.45) / 0.55
        grad.putpixel((0, yy), int(255 * a))
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, grad.resize((W, H)))
    d = ImageDraw.Draw(img)
    label = info["type"] + (f" · {info['district']}" if info["district"] else "")
    lw = d.textlength(label, font=font("Manrope-Bold", 30)) + 62 * 0.9
    pill(d, (W - lw) / 2, 64, 62, label, 30)
    l1 = info["cover"]
    l2 = (info["name"] or info["district"] or "Казань").upper()
    f = min(fit_font(d, l1, W - 120, 140), fit_font(d, l2, W - 120, 140), key=lambda x: x.size)
    d.text((60, 930), l1, font=f, fill=WHITE, anchor="ls")
    d.text((60, 930 + f.size * 1.08), l2, font=f, fill=WHITE, anchor="ls")
    x, y = 60, 1110
    x += pill(d, x, y, 84, price_line(info), 38) + 14
    if info["pets"] == "можно":
        x += pill(d, x, y, 84, "можно с животными", 38) + 14
    elif info["deposit"] == "без залога":
        x += pill(d, x, y, 84, "без залога", 38) + 14
    d.ellipse((x, y, x + 84, y + 84), fill=ACCENT)
    arrow(d, x + 42, y + 42, 18)
    d.text((60, 1292), LOGO, font=font("Unbounded-Black", 34), fill=WHITE, anchor="ls")
    return img


def slide_collage(info, ims, num):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W / 2, 82), LOGO, font=font("Unbounded-Black", 34), fill=INK, anchor="ms")
    m, g = 52, 12
    iw = W - 2 * m
    tw = (iw - g) / 2
    cover_fit(img, ims[0], m, 112, tw, 500)
    cover_fit(img, ims[1], m + tw + g, 112, tw, 500)
    bw = (iw - 2 * g) / 3
    for i in range(3):
        cover_fit(img, ims[2 + i], m + i * (bw + g), 624, bw, 400)
    d.text((m, 1140), info["title"], font=fit_font(d, info["title"], iw, 120), fill=INK, anchor="ls")
    where = ", ".join(x for x in [info["name"], info["address"]] if x) or info["district"]
    f = font("Manrope-Medium", 42)
    while d.textlength(where, font=f) > iw - 240 and len(where) > 10:
        where = where[:-2].rstrip(" ,") + "…"
    d.text((m, 1222), where, font=f, fill=INK, anchor="ls")
    d.text((m, 1285), f"{info['price']}₽/мес" + (" + КУ" if info["ku"] else ""), font=f, fill=INK, anchor="ls")
    cx = W - m - 170
    d.ellipse((cx - 26, 1244, cx + 26, 1296), fill=ACCENT)
    d.text((cx, 1270), str(num), font=font("Manrope-Bold", 30), fill=WHITE, anchor="mm")
    x = W - m - 134
    d.rounded_rectangle((x, 1244, x + 134, 1296), radius=26, fill=ACCENT)
    arrow(d, x + 67, 1270, 35, width=3)
    return img


def slide_details(info, im):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W / 2, 82), LOGO, font=font("Unbounded-Black", 34), fill=INK, anchor="ms")
    m = 52
    iw = W - 2 * m
    cover_fit(img, im, m, 112, iw, 480)
    d.text((m, 700), "УСЛОВИЯ", font=font("Unbounded-Black", 88), fill=INK, anchor="ls")
    rows = [("Аренда", price_line(info)), ("Залог", info["deposit"]), ("Комиссия", info["commission"]),
            ("Площадь", f"{info['area']} м²" if info["area"] else ""), ("Этаж", info["floor"]),
            ("Животные", info["pets"])]
    rows = [r for r in rows if r[1]][:6]
    y = 745
    fl, fv = font("Manrope-Medium", 38), font("Manrope-Bold", 38)
    for a, b in rows:
        d.line((m, y, W - m, y), fill=LINE, width=2)
        d.text((m, y + 50), a, font=fl, fill=GREY, anchor="ls")
        d.text((W - m, y + 50), b, font=fv, fill=INK, anchor="rs")
        y += 72
    d.line((m, y, W - m, y), fill=LINE, width=2)
    pill(d, m, 1224, 84, f"Объект {info['object']} · кейби.рф", 36)
    return img


def render(info, outdir):
    ims = [load_img(u) for u in info["photos"]]
    slides = [slide_cover(info, ims[:4]), slide_collage(info, ims[:5], 1)]
    if len(ims) >= 10:
        slides.append(slide_collage(info, ims[5:10], 2))
    slides.append(slide_details(info, ims[-1]))
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("*.jpg"):
        f.unlink()
    for i, s in enumerate(slides, 1):
        s.save(outdir / f"{i}.jpg", "JPEG", quality=90, optimize=True)
    (outdir / "caption.txt").write_text(caption(info), encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(slides)


def caption(info):
    head = f"{info['type']}" + (f", {info['name']}" if info["name"] else "")
    lines = [head, ""]
    place = ", ".join(x for x in [info["address"], info["district"]] if x)
    if place:
        lines.append(f"📍 {place}")
    lines.append(f"💰 {info['price']} ₽/мес" + (" + КУ" if info["ku"] else ""))
    if info["deposit"]:
        lines.append(f"🔐 Залог: {info['deposit']}")
    if info["commission"]:
        lines.append(f"🤝 Комиссия: {info['commission']}")
    extra = " · ".join(x for x in [f"{info['area']} м²" if info["area"] else "", f"этаж {info['floor']}" if info["floor"] else ""] if x)
    if extra:
        lines.append(f"🏢 {extra}")
    if info["pets"] == "можно":
        lines.append("🐾 Можно с животными")
    lines += ["", f"Номер объекта: {info['object']}",
              "Больше проверенных квартир — на кейби.рф, сервисе безопасной долгосрочной аренды в Казани.", "",
              "#аренда #арендаказань #квартиравказани #снятьквартиру #казань #кейби"]
    return "\n".join(lines)


# ---------------------------------------------------------------- состояние

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"posted": []}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- Instagram

def ig(method, path, **params):
    params["access_token"] = os.environ["IG_TOKEN"]
    r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"{path}: {r.status_code} {r.text}")
    return r.json()


def publish(obj):
    d = POSTS / obj
    files = sorted(d.glob("*.jpg"), key=lambda p: int(p.stem))
    urls = [f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/posts/{obj}/{f.name}" for f in files]
    cap = (d / "caption.txt").read_text(encoding="utf-8")
    # ждём, пока картинки станут доступны по публичной ссылке
    for u in urls:
        for _ in range(20):
            if requests.head(u, timeout=20).status_code == 200:
                break
            time.sleep(6)
    children = [ig("POST", "me/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    cont = ig("POST", "me/media", media_type="CAROUSEL", children=",".join(children), caption=cap)["id"]
    for _ in range(30):
        st = ig("GET", cont, fields="status_code").get("status_code")
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise RuntimeError("контейнер карусели в статусе ERROR")
        time.sleep(5)
    res = ig("POST", "me/media_publish", creation_id=cont)
    st = load_state()
    st["posted"].append({"object": obj, "media_id": res.get("id"), "ts": int(time.time())})
    save_state(st)
    print("published", obj, res)


def refresh():
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": os.environ["IG_TOKEN"]}, timeout=30)
    r.raise_for_status()
    print(r.json()["access_token"])


# ---------------------------------------------------------------- main

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if cmd == "prepare":
        done = {p["object"] for p in load_state()["posted"]}
        listings = [l for l in collect_listings() if l["object"] not in done]
        if not listings:
            print("нет новых объявлений"); return
        info = listings[-1]  # самое свежее
        n = render(info, POSTS / info["object"])
        print(f"prepared {info['object']} ({n} slides)")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"object={info['object']}\n")
    elif cmd == "publish":
        publish(sys.argv[2])
    elif cmd == "refresh":
        refresh()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
