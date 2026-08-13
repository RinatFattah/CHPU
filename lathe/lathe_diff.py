#!/usr/bin/env python3
"""
lathe_diff.py — ОСЕВОЙ анализатор точения: где именно по z деталь недорезана
или зарезана и на сколько миллиметров по радиусу.

Зачем отдельно от `cam/step_diff.py`. Тот отвечает на вопрос «сколько всего» и
даёт пятна-зоны; для тела вращения этого мало: дефект точения — это ПОЯС по z,
а суммарный объём одинаково хорошо описывает и равномерную плёнку, и
локальный зарез на конусе. Плюс шаг воксельной сетки там ~0.5 мм, а ошибки
порядка радиуса при вершине резца (0.4 мм) на таком шаге не видны вовсе.

Здесь считаются ДВЕ независимые вещи: тот же воксельный рей-кастинг (объёмы,
раскладка по поясам z) и профиль r(z) по осевым сечениям (точность микронная).

CLI:  python lathe/lathe_diff.py деталь.step результат.step [отчёт.md] [--pitch 0.25]
API:  lathe_diff.analyse(part, result) -> dict

────────────────────────────────────────────────────────────────────────────────
`z_shift` — ТОЛЬКО ИНСТРУМЕНТ АНАЛИЗА. Он сдвигает результат вдоль оси перед
сверкой и отвечает на вопрос «если бы единственной бедой было смещение на δz,
совпало бы остальное». В ПАЙПЛАЙН ОН НЕ ВХОДИТ И ВХОДИТЬ НЕ ДОЛЖЕН:

  * генерация, оба симулятора и конфиг о нём не знают — проверено grep'ом,
    он живёт ровно в двух файлах, этом и `cam/lathe_diff_worker.py`;
  * умолчание 0.0 и НЕ читается из конфига: не должно существовать настройки,
    которой можно молча включить сдвиг на всех прогонах;
  * CLI-флага намеренно нет — вызывается только из Python, чтобы случайно не
    попасть в пакетный прогон и не подкрасить опубликованные цифры.

Сдвинутое число нельзя выдавать за результат программы: металл от сдвига
никуда не девается. Это ответ на диагностический вопрос, а не оценка детали.
────────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from cam import freecad_cam

_WORKER = os.path.join(_ROOT, "cam", "lathe_diff_worker.py")
_DEP = os.path.join(_ROOT, "cam", "freecad_diff_worker.py")

_NUM = r"(-?\d+(?:\.\d+)?)"
_RE_TURNS = re.compile(rf"turns Z\s*{_NUM}\s*\.\.\s*{_NUM}")
_RE_BORES = re.compile(rf"bores to Z\s*{_NUM}")
_RE_DRILLS = re.compile(rf"drills to Z\s*{_NUM}")
_RE_BORE_D = re.compile(r"boring bar, to D\s*(\d+(?:\.\d+)?)")
_RE_DRILL_D = re.compile(r"twist drill D\s*(\d+(?:\.\d+)?)")
_RE_OP = re.compile(r"\(Begin operation:\s*([A-Za-z0-9_]+)\)")
_RE_XZ = re.compile(r"([XZ])(-?\d+(?:\.\d+)?)")
_RE_G = re.compile(r"^G(\d+)")


def limits_from_moves(text):
    """Докуда установ РЕАЛЬНО режет — по рабочим ходам программы.

    Шапка пишет `turns Z 0..-20.30` — это z_split, ПРОЕКТНАЯ граница передачи
    работы. Резец идёт дальше неё на величину перекрытия (`LATHE_SETUP_OVERLAP`):
    на 14-31A чистовой проход доходит до −21.90, то есть срезает верхние 1.9 мм
    шестигранника. Если резать проверку по шапке, эта работа выпадает из счёта
    — установ её сделал, а его за неё не спрашивают.

    Отрезка исключается: её ход уходит за торец детали (−49.5) и границей быть
    не может. Сверло и расточной дают ВНУТРЕННЮЮ границу; ниже неё отверстие
    намеренно оставлено в размере сверла для следующего установа.
    """
    op, x, z = "", None, None
    outer = bore = drill = None
    r_bore = 0.0
    for raw in text.splitlines():
        s = raw.strip()
        m = _RE_OP.match(s)
        if m:
            op = m.group(1).lower()
            continue
        mg = _RE_G.match(s)
        if not mg:
            continue
        code = int(mg.group(1))                    # G0/G00, G1/G01, G2/G02…
        if code > 3:
            continue                               # G18, G54, G95, G97 — не ход
        z_prev = z
        for axis, val in _RE_XZ.findall(s):        # координаты модальные
            if axis == "X":
                x = float(val)
            else:
                z = float(val)
        if z is None or code == 0:
            continue
        if "partoff" in op:
            continue
        if "bore" in op:
            bore = z if bore is None else min(bore, z)
            if x:
                r_bore = max(r_bore, x / 2.0)
        elif "drill" in op:
            if "center" not in op:
                drill = z if drill is None else min(drill, z)
        elif z_prev is not None and abs(z - z_prev) > 1e-6:
            # ПРОДОЛЬНЫЙ ход, и только он. Врезание на одном z (отрезка, канавка,
            # подрезка торца) осевую границу зоны не задаёт: отрезной уходит за
            # торец детали (у завода на z −51) и утащил бы границу за собой.
            # В чужой программе на имя операции полагаться нельзя — импортёр
            # метит отрезку тем же `Turn`, что и точение.
            outer = z if outer is None else min(outer, z)
    return outer, (bore if bore is not None else drill), r_bore


def limits_from_gcode(path):
    """Зона ответственности установа — из шапки его же программы.

    Генератор пишет её сам, например:

        (SETUP 1 of 2: from bar, Z0 = right face, turns Z 0..-20.30,
         drills to Z-29.00, bores to Z-25.50, parts off at Z-48.00)
        (Tool T5: boring bar, to D11.50 mm)

    Брать границу отсюда, а не пересчитывать по конфигу, — единственный способ
    гарантировать, что проверяется ровно то, что программа делала: конфиг мог
    поменяться после прогона, а шапка лежит внутри проверяемого файла.

    Отверстие получает СВОЮ границу и она обычно глубже наружной: установ
    растачивает дальше, чем точит. Ниже границы расточки отверстие намеренно
    оставлено в размере сверла — это работа следующего установа, не дефект.

    Возвращает {} , если программа односоставная (шапки установа нет).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    m = _RE_TURNS.search(text)
    if not m:
        return {}
    z_head = min(float(m.group(1)), float(m.group(2)))

    # Границы берём ПО ХОДАМ (см. limits_from_moves); шапка остаётся сверкой.
    z_out, z_in, r_moves = limits_from_moves(text)
    out = {"z_limit": z_out if z_out is not None else z_head}
    if z_out is not None and abs(z_out - z_head) > 0.05:
        print(f"[ldiff] шапка обещает точение до z {z_head:.2f}, ходы идут до "
              f"{z_out:.2f} — беру ходы (разница = перекрытие установов)")

    mb, md = _RE_BORES.search(text), _RE_DRILLS.search(text)
    dm = _RE_BORE_D.search(text) or _RE_DRILL_D.search(text)
    r = r_moves or (float(dm.group(1)) / 2.0 if dm else 0.0)
    z_bore = z_in if z_in is not None else (
        float(mb.group(1)) if mb else (float(md.group(1)) if md else None))
    if r and z_bore is not None:
        out["z_limit_bore"] = z_bore
        out["bore_radius"] = r
    return out


def analyse(part_path, result_path, json_path=None, pitch=None, z_bin=None,
            angles=6, prof_step=0.05, min_thickness=None, z_limit=None,
            z_limit_bore=None, bore_radius=None, z_shift=0.0, timeout=1800,
            film=None, unreachable=None):
    """Возвращает dict с by_z и profile (см. cam/lathe_diff_worker.py).

    Умолчания берутся из конфига: `LATHE_DIFF_PITCH`, `LATHE_DIFF_Z_BIN` и общий
    с фрезеровкой допуск `DIFF_MIN_THICKNESS`.

    `unreachable` — [(z_hi, z_lo, «почему»)] в раме ДЕТАЛИ: зоны, которые сама
    программа объявила недостижимыми выданным набором (канавка уже наличной
    пластины). Пояса, попавшие в них, помечаются `unreachable` и выносятся из
    приёмки отдельной строкой. Это не сокрытие: металл там остаётся и на
    станке, но отвечает за него ЗАКУПКА ИНСТРУМЕНТА, а не траектория, и
    смешивать это с дефектом программы в одном числе нельзя.
    """
    if pitch is None:
        pitch = float(getattr(config, "LATHE_DIFF_PITCH", 0.25))
    if z_bin is None:
        z_bin = float(getattr(config, "LATHE_DIFF_Z_BIN", 1.0))
    if min_thickness is None:
        min_thickness = float(getattr(config, "DIFF_MIN_THICKNESS", 0.0))
    if film is None:                    # поправка на плёнку моста в ISV, см. конфиг
        film = (float(getattr(config, "LATHE_DIFF_FILM_FACTOR", 0.41))
                * float(getattr(config, "LATHE_NOSE_RADIUS", 0.4))
                if getattr(config, "LATHE_DIFF_FILM", True) else 0.0)
    fc = freecad_cam.find_freecadcmd()
    if not fc:
        raise RuntimeError("freecadcmd не найден (укажите FREECAD_CMD в конфиге)")

    tdir = tempfile.gettempdir()
    pid = os.getpid()
    # входы — в ASCII-копии с расширением .stp: 8.3-имя обрезает «.step» до
    # «.STE», и OCCT перестаёт узнавать формат
    part_tmp = os.path.join(tdir, f"lathe_diff_part_{pid}.stp")
    res_tmp = os.path.join(tdir, f"lathe_diff_res_{pid}.stp")
    shutil.copyfile(part_path, part_tmp)
    shutil.copyfile(result_path, res_tmp)

    out_json = json_path or os.path.join(tdir, f"lathe_diff_{pid}.json")
    params = {
        "part": part_tmp,
        "result": res_tmp,
        "json_path": freecad_cam._ascii_safe(
            os.path.dirname(os.path.abspath(out_json)) or ".") + os.sep
            + os.path.basename(out_json),
        "pitch": pitch, "z_bin": z_bin,
        "angles": angles, "prof_step": prof_step,
        "min_thickness": min_thickness,
        "z_limit": z_limit, "z_limit_bore": z_limit_bore,
        "bore_radius": bore_radius, "z_shift": z_shift, "film": float(film),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(params, tmp)
        params_path = tmp.name

    # воркер импортирует ZCaster из фрезерного — оба файла должны лежать рядом
    worker = freecad_cam._ascii_safe(_WORKER)
    if not worker.isascii():
        worker = os.path.join(tdir, "lathe_diff_worker.py")
        shutil.copyfile(_WORKER, worker)
        shutil.copyfile(_DEP, os.path.join(tdir, "freecad_diff_worker.py"))

    try:
        proc = subprocess.run(
            [fc, worker], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "LATHE_DIFF_PARAMS": params_path,
                 "QT_QPA_PLATFORM": "offscreen"},
            timeout=timeout,
        )
    finally:
        for f in (params_path, part_tmp, res_tmp):
            try:
                os.unlink(f)
            except OSError:
                pass

    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    if not any("[ldiff] OK" in l for l in lines) or not os.path.exists(out_json):
        tail = "\n".join(l for l in lines if "[ldiff]" in l or "rror" in l)[-800:]
        raise RuntimeError(f"анализ не построился (код {proc.returncode}). {tail}")
    with open(out_json, encoding="utf-8") as f:
        data = json.load(f)
    _add_profile_dr(data)
    if unreachable:
        _mark_unreachable(data, unreachable)
    if json_path:                           # пометки должны попасть и в файл
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    if json_path is None:
        try:
            os.unlink(out_json)
        except OSError:
            pass
    return data


def _slopes(geo, win=0.3):
    """Уклон образующей к оси В КАЖДОЙ ТОЧКЕ профиля, ° (0 — цилиндр, 90 — торец).

    Окном ±win, а не по соседям: профиль снят шагом 0.05 мм, и точка к точке
    шум наклона больше самого наклона.

    Поточечно, а не в среднем по поясу: пояс шириной 1 мм часто накрывает и
    цилиндр, и конус, и средний уклон не годится ни для того, ни для другого.
    На 14-31A именно такие стыки давали остаточные 0.015 мм.
    """
    zs = [z for z, _ in geo]
    out = []
    n = len(geo)
    j0 = 0
    for i, (z, _) in enumerate(geo):
        while j0 < n and zs[j0] < z - win:
            j0 += 1
        j1 = i
        while j1 + 1 < n and zs[j1 + 1] <= z + win:
            j1 += 1
        a, b = geo[max(j0, 0)], geo[min(j1, n - 1)]
        dz, dr = b[0] - a[0], b[1] - a[1]
        out.append(90.0 if not dz
                   else math.degrees(math.atan2(abs(dr), abs(dz))))
    return out


def _add_profile_dr(data):
    """Добавить поясам отклонение по радиусу, СНЯТОЕ С ПРОФИЛЯ.

    Зачем, если воксельное уже есть. У воксельной половины систематический
    сдвиг: сетка 0.1 мм, граница тела попадает между плоскостями, и парность
    луча округляет наружу. На 14-31A это ровные +0.030 мм на КАЖДОМ поясе —
    воксели дают сырое −0.191 там, где профиль даёт −0.161. После поправки на
    плёнку моста (0.164) профиль показывает +0.003, то есть номинал, а воксели
    −0.027, и эти три сотых по всей поверхности набирали 90 мм³ «зареза»,
    которого нет.

    Поэтому приёмку по радиусу считаем по профилю — он на теле вращения точнее
    на порядок. Воксели остаются за объёмами и зонами: там, где профиля нет
    (фасетное тело — осевое сечение на нём не строится, см. I15), возвращаемся
    к ним, и это честно помечено полем `dr_source`.

    Правило проекта: обе половины на теле вращения обязаны сходиться. Расхождение
    между ними кладётся в `dr_halves_gap_mm` — по нему видно, когда метод врёт.
    """
    prof = [p for p in (data.get("profile") or [])
            if p.get("res_rmax") is not None and p.get("part_rmax") is not None]
    if not prof:
        return
    film = float(data.get("film_mm") or 0.0)
    # уклон образующей ДЕТАЛИ — по нему толщина зареза считается по нормали
    geo = sorted((p["z"], p["part_rmax"]) for p in (data.get("profile") or [])
                 if p.get("part_rmax") is not None)
    slope_at = dict(zip((z for z, _ in geo), _slopes(geo))) if geo else {}

    def _keep(raw_i, z_i, apply_film):
        """Отклонение точки после порога: зарез тоньше плёнки не в счёт."""
        if raw_i >= 0 or not apply_film or not film:
            return raw_i, 0.0
        th_i = slope_at.get(z_i, 0.0)
        c = math.cos(math.radians(min(th_i, 75.0)))
        excess = (-raw_i) * c - film
        return ((-(excess / c) if excess > 0 else 0.0),
                min(-raw_i, film / c))

    for b in data.get("by_z") or []:
        lo, hi = min(b["z0"], b["z1"]), max(b["z0"], b["z1"])
        pts = [p for p in prof if lo <= p["z"] <= hi]
        vals = [p["res_rmax"] - p["part_rmax"] for p in pts]
        if not vals:
            b["dr_source"] = "воксели"
            continue
        raw = sum(vals) / len(vals)
        # ПЛЁНКА — ЭТО ЗАРЕЗ, И ЕЁ НЕ ВЫЧИТАЮТ, А НЕ СЧИТАЮТ.
        #
        # Смещение точки отслеживания резца в ISV (I19) снимает лишний ровный
        # слой по ВСЕЙ детали — по нормали к поверхности, а не по радиусу.
        # Поэтому порог ставится на толщину зареза ПО НОРМАЛИ: |dr|·cos θ, где
        # θ — уклон образующей к оси. Тоньше порога — не считаем вовсе; толще —
        # считаем ТОЛЬКО превышение, чтобы настоящий зарез не спрятался.
        #
        # Почему порогом, а не прибавкой к dr (как было раньше): прибавка
        # умеет из зареза сделать НЕДОРЕЗ, если промахнулась. На цилиндрах она
        # это и делала — +0.003 мм фантомного недореза там, где ровно номинал.
        # Порог такого не может: недорез он не трогает вовсе.
        #
        # Обратная сторона, её надо помнить: там, где металл ОСТАЛСЯ, плёнка
        # его тоже подъела, и мы этого не восстанавливаем. Недорез читается
        # заниженным не больше чем на толщину плёнки.
        sl = [slope_at.get(p["z"], 0.0) for p in pts]
        b["slope_deg"] = round(sum(sl) / len(sl), 1) if sl else None
        b["dr_prof_mm"] = round(raw, 4)
        kept, ignored = zip(*(_keep(p["res_rmax"] - p["part_rmax"], p["z"],
                                    b.get("film_applied")) for p in pts))
        dr_band = sum(kept) / len(kept)
        # ПОЛ РАЗРЕШЕНИЯ. Профиль снят шагом 0.05 мм с фасетного тела, у
        # которого своя хорда: отличить 0.015 мм от нуля метод не может. Всё,
        # что мельче пола, и есть ноль — иначе метрика показывает шум, а агент
        # начинает его чинить (runs/114: Kimi ради -0.0155 мм сняла исправный
        # чистовой резец и получила -0.397).
        res = float(getattr(config, "LATHE_DIFF_RESOLUTION", 0.03) or 0.0)
        if res and abs(dr_band) < res:
            dr_band = 0.0
        b["dr_prof_fixed_mm"] = round(dr_band, 4)
        if any(ignored):
            b["film_ignored_mm"] = round(sum(ignored) / len(ignored), 4)
        b["dr_source"] = "профиль"
        vox = b.get("dr_fixed_mm")
        if vox is not None:
            b["dr_halves_gap_mm"] = round(b["dr_prof_fixed_mm"] - float(vox), 4)


def _mark_unreachable(data, zones):
    """Пометить пояса, попавшие в зоны «недостижимо выданным набором».

    Пояс метится по ПЕРЕСЕЧЕНИЮ с зоной, а не по попаданию центра: канавка
    бывает уже пояса (на 14-31A — 0.9 мм при поясе 1 мм), и по центру она бы
    не поймалась.
    """
    tot = 0.0
    for b in data.get("by_z") or []:
        lo, hi = min(b["z0"], b["z1"]), max(b["z0"], b["z1"])
        for z_hi, z_lo, *why in zones:
            zlo, zhi = min(z_hi, z_lo), max(z_hi, z_lo)
            if hi > zlo and lo < zhi:
                b["unreachable"] = True
                b["unreachable_why"] = (why[0] if why else
                                        "недостижимо выданным набором")
                tot += float(b.get("under_mm3") or 0) + float(
                    b.get("over_mm3") or 0)
                break
    data["unreachable_mm3"] = round(tot, 1)
    data["unreachable_zones"] = [list(z) for z in zones]


def bands(prof, tol=0.02):
    """Профиль → сплошные участки с одним знаком отклонения.

    Возвращает [{"z0","z1","sign","dr_peak","dr_mean","axisym"}]. Участки короче
    prof_step тут не появляются: соседние точки с тем же знаком склеиваются.
    """
    out = []
    cur = None
    for row in prof:
        dr = row.get("dr")
        if dr is None:
            continue
        sign = 0 if abs(dr) < tol else (1 if dr > 0 else -1)
        ax = row.get("axisym", True)
        if cur and cur["sign"] == sign and cur["axisym"] == ax:
            cur["z1"] = row["z"]
            cur["vals"].append(dr)
        else:
            if cur:
                out.append(cur)
            cur = {"z0": row["z"], "z1": row["z"], "sign": sign,
                   "axisym": ax, "vals": [dr]}
    if cur:
        out.append(cur)
    for b in out:
        v = b.pop("vals")
        b["dr_peak"] = round(max(v, key=abs), 3)
        b["dr_mean"] = round(sum(v) / len(v), 3)
        b["length"] = round(abs(b["z1"] - b["z0"]), 2)
    return out


def report(data, min_len=0.15, tol=0.02):
    """Человекочитаемый разбор: пояса по z + участки отклонения профиля.

    `tol` — радиальный допуск (мм), по которому режутся участки профиля. Он
    НАМЕРЕННО мельче общего `DIFF_MIN_THICKNESS`: тот работает вдоль оси и в
    масштабе фрезеровки, а по радиусу у точения интересны десятки микрон.
    """
    L = []
    L.append(f"Метод: {data['method']}")
    L.append(f"Деталь {data['part_volume_mm3']:.0f} мм³, "
             f"результат {data['result_volume_mm3']:.0f} мм³, "
             f"z {data['z_range'][0]}..{data['z_range'][1]}")
    if data.get("z_limit") is not None:
        lim = f"z ≥ {data['z_limit']:.2f}"
        if data.get("z_limit_bore") is not None:
            lim += (f", отверстие (r ≤ {data['bore_radius']:.2f}) "
                    f"z ≥ {data['z_limit_bore']:.2f}")
        chk, skp = data.get("checked_volume_mm3", 0), data.get("skipped_volume_mm3", 0)
        share = 100.0 * chk / (chk + skp) if (chk + skp) else 0.0
        L.append(f"Зона установа: {lim} — проверено {chk:.0f} мм³ детали "
                 f"из {chk + skp:.0f} ({share:.0f} %), остальное оставлено "
                 f"следующему установу и в счёт не идёт")
    L.append(f"Недорез {data['undercut_total_mm3']:.1f} мм³, "
             f"зарез {data['overcut_total_mm3']:.1f} мм³ "
             f"(воксели, БЕЗ поправок)")
    if data.get("film_mm"):
        L.append(f"**С поправкой на плёнку моста {data['film_mm']:.3f} мм по "
                 f"радиусу: недорез {data['undercut_fixed_mm3']:.1f} мм³, "
                 f"зарез {data['overcut_fixed_mm3']:.1f} мм³** (по поясам)")
    if data.get("thin_film_mm3"):
        L.append(f"Отсечено фильтром толщины вдоль оси "
                 f"({data.get('min_thickness_mm')} мм): "
                 f"{data['thin_film_mm3']:.1f} мм³")
    if data.get("unreachable_zones"):
        zs = "; ".join(f"z {min(z[0], z[1]):.2f}..{max(z[0], z[1]):.2f}"
                       for z in data["unreachable_zones"])
        L.append(f"**Из них НЕДОСТИЖИМО ВЫДАННЫМ НАБОРОМ: "
                 f"{data.get('unreachable_mm3', 0):.1f} мм³** — {zs}. "
                 f"Металл там остаётся и на станке; отвечает за это подбор "
                 f"инструмента (нужна более узкая канавочная пластина), а не "
                 f"траектория, поэтому в приёмку это не идёт и считается "
                 f"отдельно.")
    L.append("")
    L.append("## Пояса по z (воксели)")
    L.append("")
    L.append("Столбец «по радиусу» — объём пояса, делённый на длину окружности: "
             "для тела вращения это точная толщина слоя. Плюс — металл остался, "
             "минус — срезано лишнее.")
    if data.get("film_mm"):
        L.append("")
        L.append(f"Столбец «с поправкой» — то же после снятия плёнки моста "
                 f"({data['film_mm']:.3f} мм). Прочерк — поправка к поясу не "
                 f"применялась: грани под ключ, торец/уступ (там расхождение "
                 f"осевое) или отверстие (у расточного своя привязка).")
        if any(b.get("dr_prof_fixed_mm") is not None
               for b in data.get("by_z") or []):
            gaps = [abs(b["dr_halves_gap_mm"]) for b in data["by_z"]
                    if b.get("dr_halves_gap_mm") is not None]
            L.append("")
            L.append(f"**ПРИЁМКА ИДЁТ ПО ПОСЛЕДНЕМУ СТОЛБЦУ** — отклонение "
                     f"снято с осевого профиля, а не с вокселей, и плёнка в "
                     f"нём НЕ ВЫЧТЕНА, А НЕ ПОСЧИТАНА: зарез тоньше "
                     f"{data['film_mm']:.3f} мм ПО НОРМАЛИ к поверхности в счёт "
                     f"не идёт, толще — считается только превышение. Недорез "
                     f"порогом не трогается вовсе. "
                     f"У воксельной половины на теле вращения систематический "
                     f"сдвиг наружу (сетка {data.get('pitch', 0.1)} мм "
                     f"округляет границу): здесь он "
                     f"{sum(gaps) / len(gaps):.3f} мм в среднем по поясам, "
                     f"максимум {max(gaps):.3f}. Воксели остаются за объёмами "
                     f"и зонами; на фасетном теле профиль не снимается, тогда "
                     f"приёмка возвращается к ним.")
    L.append("")
    has_prof = any(b.get("dr_prof_fixed_mm") is not None
                   for b in data.get("by_z") or [])
    hdr = "| z от | z до | r ном. | недорез, мм³ | зарез, мм³ | по радиусу, мм |"
    sep = "|---:|---:|---:|---:|---:|---:|"
    if data.get("film_mm"):
        hdr += " с поправкой, мм |"
        sep += "---:|"
    if has_prof:
        hdr += " **ПО ПРОФИЛЮ, мм** |"
        sep += "---:|"
    L.append(hdr + " |")
    L.append(sep + "---|")
    for b in data["by_z"]:
        if b["under_mm3"] < 0.05 and b["over_mm3"] < 0.05:
            continue
        rn = f"{b['r_nom']:.2f}" if b.get("r_nom") is not None else "—"
        dr = b.get("dr_under_mm", 0.0) + b.get("dr_over_mm", 0.0)
        drs = "—" if "r_nom" not in b else f"{dr:+.3f}"
        if data.get("film_mm"):
            drs += (f" | {b['dr_fixed_mm']:+.3f}" if b.get("film_applied")
                    else " | —")
        if has_prof:
            dp = b.get("dr_prof_fixed_mm")
            drs += f" | **{dp:+.3f}**" if dp is not None else " | —"
        what = []
        if b.get("zone"):
            what.append(b["zone"])
        if b.get("axisym") is False:
            what.append("грани под ключ")
        if b.get("face"):
            what.append("торец/уступ — величина не радиальная")
        L.append(f"| {b['z0']:.1f} | {b['z1']:.1f} | {rn} | {b['under_mm3']:.2f} "
                 f"| {b['over_mm3']:.2f} | {drs} | {', '.join(what)} |")
    L.append("")
    if not data.get("profile"):
        L.append("## Отклонение профиля")
        L.append("")
        L.append(f"НЕ СНЯТО: {data.get('profile_note') or 'нет данных'}")
        return "\n".join(L)
    L.append(f"## Отклонение профиля (|dr| > {tol} мм, участки от {min_len} мм)")
    L.append("")
    L.append("| z от | z до | длина | dr пик | dr сред. | что |")
    L.append("|---:|---:|---:|---:|---:|---|")
    for b in bands(data["profile"], tol):
        if b["sign"] == 0 or b["length"] < min_len:
            continue
        if not b["axisym"]:
            what = "грани под ключ (не точение)"
        else:
            what = "НЕДОРЕЗ" if b["sign"] > 0 else "ЗАРЕЗ"
        L.append(f"| {b['z1']:.2f} | {b['z0']:.2f} | {b['length']:.2f} "
                 f"| {b['dr_peak']:+.3f} | {b['dr_mean']:+.3f} | {what} |")
    return "\n".join(L)


def main():
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description="осевой анализ точения")
    ap.add_argument("part")
    ap.add_argument("result")
    ap.add_argument("out", nargs="?", help="отчёт .md (рядом ляжет .json)")
    ap.add_argument("--pitch", type=float, help="шаг вокселей, мм (конфиг)")
    ap.add_argument("--z-bin", type=float, help="ширина пояса, мм (конфиг)")
    ap.add_argument("--angles", type=int, default=6, help="сечений на 90°")
    ap.add_argument("--prof-step", type=float, default=0.05, help="шаг профиля, мм")
    ap.add_argument("--min-thickness", type=float,
                    help="допуск по толщине вдоль оси, мм (конфиг)")
    ap.add_argument("--tol", type=float, default=0.02,
                    help="радиальный порог отклонения профиля, мм")
    ap.add_argument("--gcode", help="программа установа: границы зоны берутся "
                                    "из её шапки (SETUP N of M ...)")
    ap.add_argument("--z-limit", type=float,
                    help="докуда установ точил снаружи (перекрывает --gcode)")
    ap.add_argument("--z-limit-bore", type=float,
                    help="докуда расточено отверстие")
    ap.add_argument("--bore-radius", type=float, help="радиус отверстия, мм")
    ap.add_argument("--film", type=float,
                    help="поправка на плёнку моста в ISV, мм по радиусу "
                         "(по умолчанию FACTOR·R из конфига)")
    ap.add_argument("--no-film", action="store_true",
                    help="без поправки на плёнку — для замера с настоящего "
                         "станка или для legacy-симулятора")
    a = ap.parse_args()
    lim = limits_from_gcode(a.gcode) if a.gcode else {}
    if a.gcode and not lim:
        print(f"[ldiff] в шапке {os.path.basename(a.gcode)} нет разметки "
              f"установа — проверяю деталь целиком")
    for key, val in (("z_limit", a.z_limit), ("z_limit_bore", a.z_limit_bore),
                     ("bore_radius", a.bore_radius)):
        if val is not None:
            lim[key] = val
    for f in (a.part, a.result):
        if not os.path.exists(f):
            print(f"Файл не найден: {f}")
            sys.exit(1)
    js = (os.path.splitext(a.out)[0] + ".json") if a.out else None
    data = analyse(a.part, a.result, js, pitch=a.pitch, z_bin=a.z_bin,
                   angles=a.angles, prof_step=a.prof_step,
                   min_thickness=a.min_thickness,
                   film=(0.0 if a.no_film else a.film), **lim)
    text = report(data, tol=a.tol)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n[ldiff] отчёт: {a.out}")


if __name__ == "__main__":
    main()
