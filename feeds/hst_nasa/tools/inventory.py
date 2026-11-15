"""Inventory Hubble images published only on NASA's sites (processing: Gladys Kober).

    python tools/inventory.py            (run from feeds/hst_nasa; wwt env python)

Sources:
  * science.nasa.gov Hubble Caldwell and Messier catalogue pages: the page's main
    image, when its credit names Kober;
  * www.nasa.gov image articles in the Hubble category (id 2729): every figure
    whose credit names Kober.
For each image it records the full-resolution URL, title, caption/summary,
credit, the object designation found in the text, and a seed position from the
SIMBAD mirror at simbad.cfa.harvard.edu (CDS Sesame was down on 2026-11-15).
Writes candidates.json. HTML/API responses are cached in _cache/.
"""

import hashlib
import html
import json
import os
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; wwt-core-catalogs hst_nasa inventory)"}
CACHE = "_cache"
SCI = "https://science.nasa.gov/mission/hubble/science/explore-the-night-sky/hubble-{cat}-catalog/"
DESIG = re.compile(
    r"\b(NGC|IC|UGCA?|ESO|PGC|LEDA|Arp|Mrk|Markarian|Abell|ACO|HH|KMHK|NGC|Messier|M|Caldwell|"
    r"Sh2|Hen|IRAS|MCG|CGCG|SDSS|PSZ\d|MACS|RX|WISE|2MASS|Terzan|Palomar|Pal|Ruprecht|Haro|Mayall|"
    r"Ton|LDN|vdB|DDO|KUG|AGC|GAMA|Leo|Sextans|Draco)\s?([0-9][0-9A-Za-z+\-.]*(?:[ -][0-9]{2,4}[A-Za-z]?)?)")


# Pages whose text gives no resolvable designation.
NAMES = {
    "nasa-caldwell-107": "NGC 6101", "nasa-caldwell-109": "NGC 3195", "nasa-caldwell-12": "NGC 6946",
    "nasa-caldwell-52": "NGC 4697", "nasa-caldwell-68": "NGC 6729", "nasa-caldwell-84": "NGC 5286",
    "nasa-caldwell-9": "Sh2-155", "nasa-caldwell-99": "NAME Coalsack Nebula",
    "nasa-hubble-views-the-dawn-of-a-sun-like-star": "HP Tau",
    "nasa-hubble-pinpoints-a-dim-starry-mini-galaxy": "And VI",
    "nasa-hubble-sees-crimson-cloud-and-stars": "LH 95",
    "nasa-hubble-sees-swarm-of-galaxies": "MACS J0329.6-0211",
}
# Not sky images (solar-system targets).
SKIP = re.compile(r"jupiter|saturn|uranus|neptune|mars|comet|asteroid", re.I)


def get(url, kind="html"):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + "." + kind)
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="replace").read()
    for i in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=180)
            if r.ok:
                open(p, "w", encoding="utf-8").write(r.text)
                time.sleep(0.7)
                return r.text
            if r.status_code in (400, 404):
                return ""
        except requests.RequestException:
            pass
        time.sleep(5 * (i + 1))
    return ""


def clean(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def figures(page):
    """(full-res image url, caption text, credit text) for each credited figure."""
    out = []
    for m in re.finditer(r'<div class="hds-credits">(.*?)</div>', page, re.S):
        credit = clean(m.group(1))
        before = page[max(0, m.start() - 9000):m.start()]
        srcset = re.findall(r'srcset="([^"]+)"', before)
        url = ""
        if srcset:
            # srcset lists the original first, without ?resize
            first = srcset[-1].split(",")[0].strip().split(" ")[0]
            url = first.split("?")[0]
        if not url:
            src = re.findall(r'src="(https://[^"?]+\.(?:jpg|jpeg|png|tif))', before)
            url = src[-1] if src else ""
        cap = re.findall(r'hds-caption-text[^>]*>(.*?)</div>', page[max(0, m.start() - 3000):m.start()], re.S)
        out.append((url, clean(cap[-1]) if cap else "", credit))
    return out


def meta(page, prop):
    m = re.search(rf'<meta (?:property|name)="{prop}" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else ""


def designations(*texts):
    found = []
    for t in texts:
        for m in DESIG.finditer(t or ""):
            d = f"{m.group(1)} {m.group(2)}".strip(" .-")
            if m.group(1) in ("M", "Messier") and not re.fullmatch(r"\d{1,3}", m.group(2)):
                continue
            if d not in found:
                found.append(d)
    return found


def simbad(name):
    t = get("https://simbad.cfa.harvard.edu/simbad/sim-id?output.format=ASCII&Ident=" + requests.utils.quote(name), "txt")
    m = re.search(r"Coordinates\(ICRS[^:]*:\s*(\d+) (\d+) ([\d.]+)\s+([+\-]\d+) (\d+) ([\d.]+)", t)
    if not m:
        m2 = re.search(r"Coordinates\(ICRS[^:]*:\s*(\d+) (\d+) ([\d.]+)\s+([+\-]\d+) (\d+)", t)
        if not m2:
            return None
        h, mi, s, d, dm = m2.groups()
        ds = "0"
    else:
        h, mi, s, d, dm, ds = m.groups()
    ra = (int(h) + int(mi) / 60 + float(s) / 3600) * 15
    sg = -1 if d.startswith("-") else 1
    dec = sg * (abs(int(d)) + int(dm) / 60 + float(ds) / 3600)
    return round(ra, 6), round(dec, 6)


def slug(url):
    return url.rstrip("/").split("/")[-1]


def main():
    cands = []
    # 1. Caldwell / Messier catalogue pages
    for cat in ("caldwell", "messier"):
        idx = get(SCI.format(cat=cat))
        links = sorted(set(re.findall(rf'href="({re.escape(SCI.format(cat=cat))}{cat}-\d+/)"', idx)))
        for u in links:
            page = get(u)
            figs = figures(page)
            if not figs or "Kober" not in figs[0][2]:
                continue
            url, cap, credit = figs[0]
            num = slug(u).split("-")[1]
            title = ("Caldwell " if cat == "caldwell" else "Messier ") + num
            desc = meta(page, "og:description") or meta(page, "description")
            body = clean(" ".join(re.findall(r"<p[^>]*>(.*?)</p>", page, re.S)[:6]))
            names = designations(cap, desc, body)
            if cat == "messier":
                names = [f"M {num}"] + names
            cands.append({"id": f"nasa-{cat}-{num}", "source": u, "image": url, "title": title,
                          "caption": cap, "summary": desc, "credit": credit, "names": names, "date": ""})
    # 2. www.nasa.gov image articles: the Hubble category, plus a site search for
    #    "Kober" (some weekly images are filed under other categories)
    items = []
    for q in ("categories=2729", "search=Kober"):
        p = 1
        while True:
            t = get(f"https://www.nasa.gov/wp-json/wp/v2/image-article?{q}&per_page=50&page={p}"
                    "&_fields=id,date,link,title,content,excerpt", "json")
            b = json.loads(t) if t else []
            if not b:
                break
            items += b
            p += 1
    if True:
        for x in items:
            c = x["content"]["rendered"]
            if "Kober" not in c:
                continue
            title = clean(x["title"]["rendered"])
            body = clean(" ".join(re.findall(r"<p[^>]*>(.*?)</p>", c, re.S)[:4]))
            for k, (url, cap, credit) in enumerate(figures(c)):
                if "Kober" not in credit or not url:
                    continue
                names = designations(title, cap, body)
                uid = "nasa-" + slug(x["link"]) + (f"-{k + 1}" if k else "")
                cands.append({"id": uid, "source": x["link"], "image": url, "title": title,
                              "caption": cap, "summary": body, "credit": credit, "names": names,
                              "date": x["date"][:10]})
    cands = [c for c in cands if not SKIP.search(c["title"] + " " + c["caption"])]
    # seeds
    for c in cands:
        if c["id"] in NAMES:
            c["names"] = [NAMES[c["id"]]] + c["names"]
        c["seed"] = None
        for n in c["names"][:4]:
            s = simbad(n)
            if s:
                c["seed"] = {"name": n, "ra": s[0], "dec": s[1]}
                break
    # de-duplicate by image URL
    seen, out = set(), []
    for c in cands:
        if c["image"] in seen:
            continue
        seen.add(c["image"])
        out.append(c)
    json.dump(out, open("candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(out)} Kober images; {sum(1 for c in out if c['seed'])} with a seed position")
    for c in out:
        if not c["seed"]:
            print("  no seed:", c["id"], c["names"][:3], "|", c["title"][:60])


if __name__ == "__main__":
    main()
