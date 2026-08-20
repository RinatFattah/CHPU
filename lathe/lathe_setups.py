#!/usr/bin/env python3
"""
lathe_setups.py — разбиение токарной работы на ДВА УСТАНОВА (перехват).

Обточить то, за что держишь, нельзя. Поэтому длинную деталь делают в два
зажима: в первом пруток зажат за хвост, точат конец детали, сверлят и отрезают;
затем деталь перехватывают за уже обточенную поверхность и делают второй конец.
Ровно так устроен заводской эталон 14-31A: `UST1.NC` точит Z 0…−20.4, `UST2.NC`
— другой конец на укороченной заготовке.

Здесь считается ТОЛЬКО геометрия разбиения:

  * `choose_split` — где передавать работу со установа на установ;
  * `truncate`     — профиль, обрезанный по этой границе;
  * `mirror`       — профиль во второй СК (деталь перевёрнута).

Система координат второго установа: `z' = z_end − z`. Правый торец детали
(z = 0, свободный в первом установе) уходит в z' = z_end, а дальний торец
(z = z_end), который в первом установе был внутри прутка, становится свободным
торцом z' = 0. Длина детали одна и та же, поэтому в обоих установах программа
пишется одинаково — меняется только профиль.

Что каждый установ делает:

| | установ 1 | установ 2 |
|---|---|---|
| точит | z ∈ [z_split, 0] | z' ∈ [z_end − z_split, 0] |
| осевое отверстие | целиком (насквозь) | не трогает |
| отрезка | да, за торцом детали | нет |
| подрезка торца | снимает припуск заготовки | снимает пенёк от отрезки |

**Заготовка второго установа — тот же пруток.** Первый установ обточил только
z ∈ [z_split, 0], а второй работает по z' ≥ z_end − z_split, что в исходных
координатах есть z ≤ z_split — то есть по нетронутому прутку. Специально
передавать профиль-заготовку от установа к установу для ГЕНЕРАЦИИ не нужно
(для симуляции — нужно, там результаты честно склеиваются).
"""


def truncate(profile, z_lo):
    """Профиль, обрезанный снизу по z_lo, с точкой ровно на границе."""
    pts = sorted(((float(z), float(r)) for z, r in profile), key=lambda t: -t[0])
    out, prev = [], None
    for z, r in pts:
        if z >= z_lo - 1e-9:
            out.append((z, r))
            prev = (z, r)
            continue
        if prev is not None and prev[0] > z_lo + 1e-9:
            t = (z_lo - prev[0]) / (z - prev[0])
            out.append((z_lo, prev[1] + (r - prev[1]) * t))
        break
    return out


def mirror(profile, z_end):
    """Профиль в СК второго установа: z' = z_end − z (деталь перевёрнута)."""
    return sorted(((z_end - float(z), float(r)) for z, r in profile),
                  key=lambda t: -t[0])


def choose_split(profile, z_end, grip, override=None, snap_window=None):
    """Где передавать работу второму установу.

    По умолчанию — СЕРЕДИНА детали: тогда каждому установу достаётся примерно
    поровну и обоим хватает вылета. Ограничение одно и жёсткое: во втором
    установе кулачки садятся на поверхность, обточенную в первом, поэтому её
    длина не может быть меньше зажима — то есть |z_split| ≥ grip.

    Дальше граница ПРИТЯГИВАЕТСЯ к ближайшей вершине профиля (уступу) в окне
    ±snap_window: передавать работу посреди цилиндра — значит оставить на нём
    след стыка двух установов, а на уступе он никому не мешает.

    Возвращает z_split (отрицательное, в исходной СК).
    """
    if override is not None:
        return float(override)

    z_split = max(z_end / 2.0, z_end + grip)     # середина, но не глубже
    z_split = min(z_split, -grip)                # и не мельче зажима
    if z_split <= z_end or z_split >= 0:
        return z_split

    win = snap_window if snap_window is not None else max(1.0, grip / 3.0)
    best = None
    for z, _ in profile:
        z = float(z)
        if not (z_end + grip <= z <= -grip):
            continue                              # вершина вне допустимого окна
        d = abs(z - z_split)
        if d <= win and (best is None or d < best[0]):
            best = (d, z)
    return best[1] if best else z_split


def split(prof, z_split, face_allowance=2.0, overlap=2.0):
    """prof → (prof1, prof2) — данные профиля для первого и второго установа.

    prof1 — деталь до границы, отверстие целиком, заготовка как была.
    prof2 — зеркальная деталь до своей границы, без отверстия, «заготовка»
            торчит на face_allowance за отрезанный торец (его подрежут).

    ПЕРЕКРЫТИЕ обязательно, стык впритык не работает. Чистовой проход идёт по
    ЭКВИДИСТАНТЕ, и у обрезанного края компенсация радиуса при вершине не даёт
    ему дойти до самой границы — остаётся полоска шириной с нос резца. Когда
    оба установа обрываются на одном z, эти полоски складываются, и на детали
    остаётся кольцо НЕТРОНУТОГО ПРУТКА (замерено: r = 17.0 при номинале 13.85
    на z −21.1…−19.5). Поэтому каждый установ заходит за границу на `overlap`;
    лишний проход идёт по уже готовой поверхности, то есть по воздуху.
    """
    z_end = min(float(z) for z, _ in prof["profile"])
    raw = prof.get("profile_raw") or prof["profile"]

    prof1 = dict(prof)
    prof1["profile"] = truncate(prof["profile"], z_split - overlap)
    prof1["profile_raw"] = truncate(raw, z_split - overlap)

    z_lim2 = z_end - z_split - overlap
    prof2 = dict(prof)
    prof2["profile"] = truncate(mirror(prof["profile"], z_end), z_lim2)
    prof2["profile_raw"] = truncate(mirror(raw, z_end), z_lim2)
    # Отверстие делается С ДВУХ СТОРОН, как на заводе: каждый установ берёт свою
    # половину, глубину ограничивает hole_depth_* (см. lathe_gcode). Раньше всё
    # отверстие шло первым установом, и сверло Ø10 уходило на 49 мм — вылет
    # L/D 4.9 против заводских 2.9.
    prof2["bore_raw"] = mirror(prof.get("bore_raw") or [], z_end)
    prof2["stock_z_top"] = float(face_allowance)
    prof2["stock_z_bottom"] = z_end
    return prof1, prof2


def split_threads(threads, z_split, z_end):
    """Резьбы → (для первого установа, для второго — уже в его СК).

    Резьба, попавшая на границу, отдаётся тому установу, в котором она лежит
    ЦЕЛИКОМ; если не лежит ни в одном — остаётся в первом и о ней сообщается.
    """
    one, two, split_over = [], [], []
    for th in (threads or []):
        z_hi = max(float(th["z_from"]), float(th["z_to"]))
        z_lo = min(float(th["z_from"]), float(th["z_to"]))
        if z_lo >= z_split:
            one.append(th)
        elif z_hi <= z_split:
            t = dict(th)
            t["z_from"] = z_end - z_hi
            t["z_to"] = z_end - z_lo
            two.append(t)
        else:
            one.append(th)
            split_over.append(th)
    return one, two, split_over
