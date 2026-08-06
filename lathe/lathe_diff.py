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
"""

import argparse
import json
import os
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


def analyse(part_path, result_path, json_path=None, pitch=None, z_bin=None,
            angles=6, prof_step=0.05, min_thickness=None, timeout=1800):
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
    L.append(f"Недорез {data['undercut_total_mm3']:.1f} мм³, "
             f"зарез {data['overcut_total_mm3']:.1f} мм³ (воксели)")
    if data.get("thin_film_mm3"):
        L.append(f"Отсечено фильтром толщины вдоль оси "
                 f"({data.get('min_thickness_mm')} мм): "
                 f"{data['thin_film_mm3']:.1f} мм³")
    L.append("")
    L.append("## Пояса по z (воксели)")
    L.append("")
    L.append("| z от | z до | недорез, мм³ | зарез, мм³ |")
    L.append("|---:|---:|---:|---:|")
    for b in data["by_z"]:
        if b["under_mm3"] < 0.05 and b["over_mm3"] < 0.05:
            continue
        L.append(f"| {b['z0']:.1f} | {b['z1']:.1f} | {b['under_mm3']:.2f} "
                 f"| {b['over_mm3']:.2f} |")
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
    a = ap.parse_args()
    for f in (a.part, a.result):
        if not os.path.exists(f):
            print(f"Файл не найден: {f}")
            sys.exit(1)
    js = (os.path.splitext(a.out)[0] + ".json") if a.out else None
    data = analyse(a.part, a.result, js, pitch=a.pitch, z_bin=a.z_bin,
                   angles=a.angles, prof_step=a.prof_step,
                   min_thickness=a.min_thickness)
    text = report(data, tol=a.tol)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n[ldiff] отчёт: {a.out}")


if __name__ == "__main__":
    main()
