#!/usr/bin/env python3
"""Turn an Apple Health export into a Training Log import file.

Usage:
  python3 apple_health_import.py export.zip [--backup training-log-backup-2026-10-05.json]
                                 [--since 2026-01-01] [--units lb mi] [-o training-log-import.json]

Reads the workouts (and heart-rate samples when a workout carries no statistics) out of
export.xml, converts them to the app's workout shape, and merges them with an existing
backup if you pass one: logged workouts that overlap an Apple workout get their average
and max heart rate filled in; Apple workouts you never logged become new entries.
Restore the output file in the app: Settings -> Restore from file. Re-running is safe,
the ids are stable so nothing is duplicated.

Only the Python standard library is used.
"""
import argparse, bisect, datetime as dt, hashlib, json, re, sys, zipfile
from xml.etree import ElementTree as ET

LB_PER_KG, KM_PER_MI = 2.20462, 1.60934
HR = 'HKQuantityTypeIdentifierHeartRate'
DIST_TYPES = {'HKQuantityTypeIdentifierDistanceWalkingRunning', 'HKQuantityTypeIdentifierDistanceCycling',
              'HKQuantityTypeIdentifierDistanceSwimming', 'HKQuantityTypeIdentifierDistanceRowing',
              'HKQuantityTypeIdentifierDistanceWheelchair', 'HKQuantityTypeIdentifierDistanceCrossCountrySkiing',
              'HKQuantityTypeIdentifierDistanceDownhillSnowSports', 'HKQuantityTypeIdentifierDistancePaddleSports'}
ENERGY = 'HKQuantityTypeIdentifierActiveEnergyBurned'

CARDIO = {  # activity -> exercise name shown in the app
    'Running': 'Run', 'Walking': 'Walk', 'Hiking': 'Hike', 'Cycling': 'Cycle', 'Rowing': 'Row (erg)',
    'Elliptical': 'Elliptical', 'StairClimbing': 'Stair climber', 'Stairs': 'Stairs', 'Swimming': 'Swim',
    'MixedCardio': 'Mixed cardio', 'JumpRope': 'Jump rope', 'HighIntensityIntervalTraining': 'HIIT',
    'CrossTraining': 'Cross training', 'HandCycling': 'Hand cycle', 'Wheelchair': 'Wheelchair',
    'CrossCountrySkiing': 'Cross-country ski', 'PaddleSports': 'Paddle', 'TrackAndField': 'Track',
}
STRENGTH = {'TraditionalStrengthTraining', 'FunctionalStrengthTraining', 'CoreTraining'}
MOBILITY = {'Yoga', 'Flexibility', 'Pilates', 'Cooldown', 'MindAndBody', 'TaiChi', 'Barre', 'Preparation'}
OTHER = {'Other', 'Play', 'Fitness', 'Dance', 'Cardio'}

def words(camel):
    return re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', camel).strip()

def activity_name(raw):
    return raw.replace('HKWorkoutActivityType', '')

def classify(act):
    if act in CARDIO: return 'Cardio', CARDIO[act]
    if act in STRENGTH: return 'Strength', None
    if act in MOBILITY: return 'Mobility', words(act)
    if act in OTHER: return 'Other', None
    return 'Sport', words(act)

def parse_date(s):  # "2026-10-03 07:12:45 -0400"
    return dt.datetime.strptime(s, '%Y-%m-%d %H:%M:%S %z')

def open_export(path):
    if path.lower().endswith('.zip'):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.endswith('export.xml') and 'cda' not in n.lower()]
        if not names: sys.exit('No export.xml inside that zip. Is it the Apple Health export?')
        return z.open(names[0])
    return open(path, 'rb')

def scan(stream, since_ts):
    """One streaming pass: workouts plus heart-rate samples (kept only when needed)."""
    workouts, hr = [], []
    root = None
    for ev, el in ET.iterparse(stream, events=('start', 'end')):
        if ev == 'start':
            if root is None: root = el
            continue
        tag = el.tag
        if tag == 'Record':
            if el.get('type') == HR:
                try:
                    ts = parse_date(el.get('startDate')).timestamp()
                    if ts >= since_ts: hr.append((ts, float(el.get('value'))))
                except (TypeError, ValueError): pass
        elif tag == 'Workout':
            try:
                start, end = parse_date(el.get('startDate')), parse_date(el.get('endDate'))
            except (TypeError, ValueError):
                el.clear(); continue
            if start.timestamp() >= since_ts:
                w = {'activity': activity_name(el.get('workoutActivityType', 'Other')), 'start': start, 'end': end,
                     'duration_min': None, 'distance': None, 'dist_unit': None, 'kcal': None, 'avg': None, 'max': None,
                     'source': el.get('sourceName', '')}
                try:
                    d = float(el.get('duration')); u = el.get('durationUnit', 'min')
                    w['duration_min'] = d / 60 if u == 's' else d * 60 if u == 'hr' else d
                except (TypeError, ValueError): pass
                try:
                    if el.get('totalDistance'): w['distance'] = float(el.get('totalDistance')); w['dist_unit'] = el.get('totalDistanceUnit', 'mi')
                except ValueError: pass
                try:
                    if el.get('totalEnergyBurned'): w['kcal'] = float(el.get('totalEnergyBurned'))
                except ValueError: pass
                for st in el.findall('WorkoutStatistics'):
                    t = st.get('type')
                    try:
                        if t == HR:
                            if st.get('average'): w['avg'] = float(st.get('average'))
                            if st.get('maximum'): w['max'] = float(st.get('maximum'))
                        elif t in DIST_TYPES and st.get('sum') and w['distance'] is None:
                            w['distance'] = float(st.get('sum')); w['dist_unit'] = st.get('unit', 'mi')
                        elif t == ENERGY and st.get('sum') and w['kcal'] is None:
                            w['kcal'] = float(st.get('sum'))
                    except ValueError: pass
                workouts.append(w)
        if root is not None and el is not root and tag in ('Record', 'Workout', 'ActivitySummary', 'Correlation', 'ClinicalRecord', 'Audiogram', 'Me', 'ExportDate'):
            el.clear()
            root.clear()
    return workouts, hr

def fill_hr_from_samples(workouts, hr):
    hr.sort()
    ts = [t for t, _ in hr]
    for w in workouts:
        if w['avg'] is not None: continue
        a, b = bisect.bisect_left(ts, w['start'].timestamp()), bisect.bisect_right(ts, w['end'].timestamp())
        vals = [v for _, v in hr[a:b]]
        if vals:
            w['avg'] = sum(vals) / len(vals); w['max'] = max(vals)

def to_app(w, wunit, dunit):
    kind, exname = classify(w['activity'])
    start_ms = int(w['start'].timestamp() * 1000)
    dur = round(w['duration_min'] or (w['end'] - w['start']).total_seconds() / 60)
    exercises = []
    if exname:
        ex = {'name': exname, 'kind': 'cardio', 'minutes': dur}
        if w['distance']:
            d = w['distance']
            if w['dist_unit'] == 'km' and dunit == 'mi': d /= KM_PER_MI
            if w['dist_unit'] == 'mi' and dunit == 'km': d *= KM_PER_MI
            if w['dist_unit'] == 'm': d = d / 1000 * (1 if dunit == 'km' else 1 / KM_PER_MI)
            ex['distance'] = round(d, 2)
        else:
            ex['distance'] = 0
        exercises.append(ex)
    note = 'Apple Health: ' + words(w['activity'])
    if w['kcal']: note += ' · %d kcal' % round(w['kcal'])
    if w['source']: note += ' · ' + w['source']
    return {
        'id': 'ah-' + hashlib.sha1(w['start'].isoformat().encode()).hexdigest()[:12],
        'date': w['start'].strftime('%Y-%m-%d'), 'startedAt': start_ms, 'durationMin': dur, 'type': kind,
        'exercises': exercises, 'avgHr': round(w['avg']) if w['avg'] else None, 'maxHr': round(w['max']) if w['max'] else None,
        'rpe': None, 'notes': note, 'weightUnit': wunit, 'distUnit': dunit, 'prs': [], 'createdAt': start_ms,
        'source': 'apple-health',
    }

def merge(existing, apple):
    """Fill heart rate on logged workouts that overlap an Apple workout; return (updates, new)."""
    updates, new, used = [], [], set()
    by_date = {}
    for w in existing: by_date.setdefault(w.get('date'), []).append(w)
    for a in apple:
        cands = [w for w in by_date.get(a['date'], []) if w['id'] not in used and not str(w.get('id', '')).startswith('ah-')]
        hit = None
        for w in cands:
            s = w.get('startedAt')
            if s and (a['startedAt'] - 30 * 60000) <= s <= (a['startedAt'] + a['durationMin'] * 60000 + 30 * 60000):
                hit = w; break
        if hit is None:
            untimed = [w for w in cands if not w.get('startedAt')]
            if untimed:
                hit = min(untimed, key=lambda w: abs((w.get('durationMin') or 0) - a['durationMin']))
        if hit is None:
            new.append(a); continue
        used.add(hit['id'])
        u = dict(hit); changed = False
        for k in ('avgHr', 'maxHr'):
            if not u.get(k) and a.get(k): u[k] = a[k]; changed = True
        if not u.get('startedAt') and a.get('startedAt'): u['startedAt'] = a['startedAt']; changed = True
        if changed: updates.append(u)
    return updates, new

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('export', help='export.zip from Apple Health (or an unzipped export.xml)')
    p.add_argument('--backup', help='a backup JSON from the app, to fill heart rate into logged workouts')
    p.add_argument('--since', help='ignore workouts before this date (YYYY-MM-DD)')
    p.add_argument('--units', nargs=2, metavar=('WEIGHT', 'DISTANCE'), default=['lb', 'mi'], help='lb|kg and mi|km, as set in the app')
    p.add_argument('-o', '--out', default='training-log-import.json')
    args = p.parse_args()
    wunit = 'kg' if args.units[0] == 'kg' else 'lb'; dunit = 'km' if args.units[1] == 'km' else 'mi'
    since_ts = dt.datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp() - 86400 if args.since else 0

    print('Reading', args.export, '...')
    workouts, hr = scan(open_export(args.export), since_ts)
    fill_hr_from_samples(workouts, hr)
    apple = [to_app(w, wunit, dunit) for w in workouts]
    apple.sort(key=lambda w: w['startedAt'])
    print('Found %d workouts (%d heart-rate samples read)' % (len(apple), len(hr)))
    if apple: print('  from %s to %s' % (apple[0]['date'], apple[-1]['date']))
    counts = {}
    for w in apple: counts[w['type']] = counts.get(w['type'], 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]): print('  %-9s %d' % (k, v))
    with_hr = sum(1 for w in apple if w['avgHr'])
    print('  %d with heart rate' % with_hr)

    out_workouts = apple
    if args.backup:
        existing = json.load(open(args.backup))
        existing = existing.get('workouts', existing) if isinstance(existing, dict) else existing
        updates, new = merge(existing, apple)
        print('Merged with %s: %d logged workouts get heart rate filled in, %d new entries' % (args.backup, len(updates), len(new)))
        out_workouts = updates + new
    json.dump({'app': 'training-log', 'version': 1, 'exportedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
               'source': 'apple-health-import', 'workouts': out_workouts}, open(args.out, 'w'), indent=1)
    print('Wrote', args.out, '-> in the app: Settings -> Restore from file')

if __name__ == '__main__':
    main()
