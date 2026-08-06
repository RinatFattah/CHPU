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
            z_limit_bore=None, bore_radius=None, z_shift=0.0, timeout=1800):
    """Возвращает dict с by_z и profile (см. cam/lathe_diff_worker.py).

    Умолчания берутся из конфига: `LATHE_DIFF_PITCH`, `LATHE_DIFF_Z_BIN` и общий
    с фрезеровкой допуск `DIFF_MIN_THICKNESS`.
    """
    if pitch is None:
        pitch = float(getattr(config, "LATHE_DIFF_PITCH", 0.25))
    if z_bin is None:
        z_bin = float(getattr(config, "LATHE_DIFF_Z_BIN", 1.0))
    if min_thickness is None:
        min_thickness = float(getattr(config, "DIFF_MIN_THICKNESS", 0.0))
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
        "bore_radius": bore_radius, "z_shift": z_shift,
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
    if json_path is None:
        try:
            os.unlink(out_json)
        except OSError:
            pass
    return data


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
             f"зарез {data['overcut_total_mm3']:.1f} мм³ (воксели)")
    if data.get("thin_film_mm3"):
        L.append(f"Отсечено фильтром толщины вдоль оси "
                 f"({data.get('min_thickness_mm')} мм): "
                 f"{data['thin_film_mm3']:.1f} мм³")
    L.append("")
    L.append("## Пояса по z (воксели)")
    L.append("")
    L.append("Столбец «по радиусу» — объём пояса, делённый на длину окружности: "
             "для тела вращения это точная толщина слоя. Плюс — металл остался, "
             "минус — срезано лишнее.")
    L.append("")
    L.append("| z от | z до | r ном. | недорез, мм³ | зарез, мм³ | по радиусу, мм | |")
    L.append("|---:|---:|---:|---:|---:|---:|---|")
    for b in data["by_z"]:
        if b["under_mm3"] < 0.05 and b["over_mm3"] < 0.05:
            continue
        rn = f"{b['r_nom']:.2f}" if b.get("r_nom") is not None else "—"
        dr = b.get("dr_under_mm", 0.0) + b.get("dr_over_mm", 0.0)
        drs = "—" if "r_nom" not in b else f"{dr:+.3f}"
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
                   min_thickness=a.min_thickness, **lim)
    text = report(data, tol=a.tol)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n[ldiff] отчёт: {a.out}")


if __name__ == "__main__":
    main()
