"""Build food-db.json for the Training Log app from USDA FoodData Central downloads.

Usage: python build_food_db.py <survey_csv_dir> <sr_legacy_csv_dir> <out.json>
Inputs are the unzipped "Survey Foods (FNDDS)" and "SR Legacy" CSV folders from
https://fdc.nal.usda.gov/download-datasets (public domain, CC0).
Values are stored per 100 g with household portions, so the app can offer a serving picker.
"""
import csv, json, os, re, sys

def rows(d, name):
    with open(os.path.join(d, name), encoding='utf-8', newline='') as f:
        yield from csv.DictReader(f)

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def r1(v): return round(v, 1) if v is not None else 0
def clean(v): return int(v) if float(v).is_integer() else v

def tidy_name(s):
    s = s.strip()
    s = re.sub(r',\s*NFS\b', '', s)
    s = re.sub(r'\bNS as to\b', 'unspecified', s)
    s = re.sub(r'\bNFS\b', 'unspecified', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,')
    def low(m):
        w = m.group(0)
        return w if w in KEEP_CAPS else w[0] + w[1:].lower()
    s = s.replace("McDONALD'S", "McDonald's")
    s = re.sub(r"Mc([A-Z])([A-Z]{2,})", lambda m: 'Mc' + m.group(1) + m.group(2).lower(), s)
    s = re.sub(r"\s*\(Includes foods for USDA's Food Distribution Program\)", '', s)
    s = re.sub(r"(?<![A-Za-z'])[A-Z][A-Z']{2,}(?![A-Za-z])", low, s)   # SR Legacy shouts brand names
    return s

def frac(a):
    a = clean(round(a, 3))
    return {0.5: '1/2', 0.25: '1/4', 0.75: '3/4', 0.333: '1/3', 0.667: '2/3', 0.125: '1/8'}.get(a, str(a))

KEEP_CAPS = {'KFC', 'USDA', 'BBQ', 'UHT', 'IHOP', 'DHA', 'ARA', 'USA', 'WIC', 'NFS', 'MRE', 'BLT', 'TGI', 'PBJ', 'GMO', 'CLA', 'MCT', 'RTD', 'RTE', 'RTF', 'RTB', 'RTS', 'STK', 'NBC', 'CVS', 'HFCS', 'A&W'}
SKIP_SR_CATS = {'Baby Foods', 'Quality Control Materials'}
SKIP_NAME = re.compile(r'^(baby ?food|infant formula|milk, human|formula,|human milk)|\binfant formula\b|\bbaby food\b|babyfood|toddler formula', re.I)

def load(d, kind):
    cats = {}
    if kind == 'sr':
        cats = {r['id']: r['description'] for r in rows(d, 'food_category.csv')}
    else:
        cats = {r['wweia_food_category']: r['wweia_food_category_description'] for r in rows(d, 'wweia_food_category.csv')}
    ids = {'kcal': {'208', '1008'}, 'p': {'203', '1003'}, 'f': {'204', '1004'}, 'c': {'205', '1005'}}
    alt_kcal = {'2047', '2048', '957', '958'}
    nut = {}
    for r in rows(d, 'food_nutrient.csv'):
        n = r['nutrient_id']; a = num(r['amount'])
        if a is None: continue
        e = nut.setdefault(r['fdc_id'], {})
        for k, s in ids.items():
            if n in s: e[k] = a
        if n in alt_kcal: e.setdefault('kcal2', a)
    por = {}
    for r in rows(d, 'food_portion.csv'):
        g = num(r['gram_weight'])
        if not g or g <= 0: continue
        if kind == 'sr':
            amt = num(r['amount']) or 1; mod = (r['modifier'] or '').strip()
            if not mod: continue
            label = frac(amt) + ' ' + mod
        else:
            label = (r['portion_description'] or '').strip()
            if not label: continue
            if label.lower() == 'quantity not specified': label = '1 serving (typical amount)'
        label = re.sub(r'\s+', ' ', re.sub(r',? \(?NFS\)?', '', label)).strip()[:60]
        por.setdefault(r['fdc_id'], []).append((int(num(r['seq_num']) or 99), label, clean(round(g, 1))))
    out = []
    for r in rows(d, 'food.csv'):
        fid = r['fdc_id']; e = nut.get(fid) or {}
        kcal = e.get('kcal', e.get('kcal2'))
        if kcal is None: continue
        cat = cats.get(r['food_category_id'], '')
        if cat.lower().startswith('not included'): cat = 'Ingredient'
        if cat in SKIP_SR_CATS or SKIP_NAME.search(r['description']) or cat.lower().startswith(('baby', 'human milk', 'formula')): continue
        name = tidy_name(r['description'])
        ps, seen = [], set()
        for _, label, g in sorted(por.get(fid, [])):
            k = label.lower()
            if k in seen: continue
            seen.add(k); ps.append([label, g])
        typical = [p for p in ps if p[0].startswith('1 serving (typical')]
        ps = [p for p in ps if not p[0].startswith('1 serving (typical')][:5]
        # default portion first: the one matching the survey's typical amount, else the first that is not a bare ounce
        first = None
        if typical:
            same = [p for p in ps if p[1] == typical[0][1]]
            first = same[0] if same else None
            if first is None: ps.append(['1 serving', typical[0][1]]); first = ps[-1]
        if first is None:
            plain = [p for p in ps if not re.match(r'^[\d./]+ (fl )?oz\b', p[0])]
            first = plain[0] if plain else None
        if first is not None: ps.remove(first); ps.insert(0, first)
        out.append({'name': name, 'cat': cat, 'kcal': clean(round(kcal)), 'p': clean(r1(e.get('p'))), 'c': clean(r1(e.get('c'))), 'f': clean(r1(e.get('f'))), 'ps': ps, 'id': int(fid)})
    return out

def main():
    sdir, ldir, outp = sys.argv[1:4]
    fn = load(sdir, 'fndds'); sr = load(ldir, 'sr')
    cats, ci, items, seen = [], {}, [], set()
    for src, lst in ((0, fn), (1, sr)):
        for it in lst:
            key = re.sub(r'[^a-z0-9]+', ' ', it['name'].lower()).strip()
            if key in seen: continue
            seen.add(key)
            if it['cat'] not in ci: ci[it['cat']] = len(cats); cats.append(it['cat'])
            items.append([it['name'], ci[it['cat']], it['kcal'], it['p'], it['c'], it['f'], it['ps'], src, it['id']])
    doc = {'version': 1, 'built': sys.argv[4] if len(sys.argv) > 4 else '',
           'note': 'USDA FoodData Central: Survey Foods (FNDDS 2021-2023) and SR Legacy (2018). Public domain. Values per 100 g; portions are [label, grams]. Row: [name, category index, kcal, protein g, carbs g, fat g, portions, source (0 FNDDS, 1 SR Legacy), FDC id].',
           'cats': cats, 'items': items}
    with open(outp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
    print('fndds', len(fn), 'sr', len(sr), 'kept', len(items), 'cats', len(cats), 'bytes', os.path.getsize(outp))

main()
