# -*- coding: utf-8 -*-
"""Техплан — что и чем обрабатывать, отдельно от того, КАК считается траектория.

Зачем. Сейчас решения технолога (какие зоны, в каком порядке, какой фрезой, с
каким припуском и делением на проходы) существуют только как поток управления
внутри `make_roughing_ops`: их нельзя ни посмотреть, ни сравнить, ни подменить.
План выносит их в данные. Дальше эти же данные становятся тем, что заполняет
агент, а исполнитель остаётся детерминированным.

Что важно на этом шаге: план ТОЛЬКО ЗАПИСЫВАЕТСЯ. Ни одна операция от его
появления не меняется — иначе двенадцать отданных заводу программ перестали бы
воспроизводиться, а это единственный регресс, который у нас есть.

Модуль намеренно без импортов FreeCAD: его читают и тесты, и хост, и будущий
оркестратор. Свойства операций достаются утиной типизацией — объект даёт
`.StartDepth`, значит берём.
"""

import json
import os

PLAN_VERSION = 1

# Класс операции FreeCAD → как эта стратегия называется у технолога.
STRATEGY = {
    "ObjectAdaptive": "выборка",
    "ObjectProfile": "контурный обход",
    "ObjectSurface": "3D-проход по поверхности",
    "ObjectEngrave": "проход по средней линии",
    "ObjectPocket": "карман витками по контуру",
    "ObjectPocketShape": "карман витками по контуру",
}


def _raw(v):
    """Quantity, float или None → float или None, БЕЗ округления."""
    if v is None:
        return None
    v = getattr(v, "Value", v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    """То же, но округлённое для вывода."""
    f = _raw(v)
    return None if f is None else round(f, 4)


def _prop(op, name):
    return _num(getattr(op, name, None))


def _feed_mm_min(tc):
    """Подача в мм/мин. У Quantity FreeCAD `.Value` во ВНУТРЕННИХ единицах —
    для скорости это мм/с, и 2000 мм/мин приходят как 33.33. Сначала пробуем
    честное преобразование, и только если его нет — множитель."""
    q = getattr(tc, "HorizFeed", None)
    if q is None:
        return None
    try:
        return round(float(q.getValueAs("mm/min")), 3)
    except Exception:
        f = _raw(q)
        return None if f is None else round(f * 60.0, 3)


class Plan:
    """Накопитель. Пишется по ходу расчёта, сериализуется в конце."""

    def __init__(self):
        self.data = {
            "версия": PLAN_VERSION,
            "вход": {},
            "фичи": [],
            "переходы": [],
            "порядок": [],
            "невыполнено": [],
        }
        self._op2feat = {}       # имя операции → id фичи
        self._ground = {}        # имя операции → список обоснований
        self._n = 0

    # ── наполнение ────────────────────────────────────────────────────────────

    def input(self, **kw):
        self.data["вход"].update({k: v for k, v in kw.items() if v is not None})

    def feature(self, op_name, klass, **kw):
        """Фича + связь с операцией, которая её обработает.

        Связь заводится ЗАРАНЕЕ, по будущему имени операции: имена
        детерминированы (`RoughSlope2`), а операция может и не родиться —
        тогда фича останется в плане без перехода, и это видно.
        """
        self._n += 1
        fid = "F%02d" % self._n
        rec = {"id": fid, "класс": klass, "операция": op_name}
        # bool — подкласс int, и без этой проверки «узкая_полоса: true»
        # превращается в 1.0
        rec.update({k: (v if isinstance(v, bool) else
                        _num(v) if isinstance(v, (int, float)) else v)
                    for k, v in kw.items() if v is not None})
        self.data["фичи"].append(rec)
        self._op2feat[op_name] = fid
        return fid

    def undone(self, шаг, причина, умолчание=None):
        rec = {"шаг": шаг, "причина": причина}
        if умолчание is not None:
            rec["принято_по_умолчанию"] = умолчание
        self.data["невыполнено"].append(rec)

    # ── сбор переходов из готовых операций ────────────────────────────────────

    def collect(self, ops):
        """Переходы вычитываются из САМИХ операций, а не ведутся параллельно.

        Параллельная бухгалтерия разошлась бы с тем, что реально построено, —
        и разошлась бы молча. Здесь источник истины один: объект операции.
        """
        for op in ops:
            name = getattr(op, "Name", None) or getattr(op, "Label", "?")
            cls = type(getattr(op, "Proxy", op)).__name__
            tc = getattr(op, "ToolController", None)
            tool = getattr(tc, "Tool", None)
            # Число ходов считается по НЕОКРУГЛЁННЫМ величинам: округление
            # слоя до сотых микрона делает частное чуть больше целого, и
            # ceil даёт лишний ход.
            r_start = _raw(getattr(op, "StartDepth", None))
            r_final = _raw(getattr(op, "FinalDepth", None))
            r_step = _raw(getattr(op, "StepDown", None))
            start, final = _prop(op, "StartDepth"), _prop(op, "FinalDepth")
            step = _prop(op, "StepDown")
            rec = {
                "id": name,
                "фича": self._feat_of(name),
                "вид": "чистовой" if name.startswith("Finish") else "черновой",
                "стратегия": STRATEGY.get(cls, cls),
                "инструмент_Ø": _num(getattr(tool, "Diameter", None)),
                "T": _num(getattr(tc, "ToolNumber", None)),
                "Z_от": start,
                "Z_до": final,
                "слой": step,
                "рабочих_ходов": self._laps(r_start, r_final, r_step),
                "припуск_XY": (_prop(op, "StockToLeave")
                               if hasattr(op, "StockToLeave")
                               else _prop(op, "OffsetExtra")),
                "шаг_строчек_%": _prop(op, "StepOver"),
                "сторона": getattr(op, "Side", None),
                "подача": _feed_mm_min(tc),
                "обороты": _num(getattr(tc, "SpindleSpeed", None)),
                "обоснование": self._ground.get(name, []),
            }
            self.data["переходы"].append({k: v for k, v in rec.items()
                                          if v is not None})
            self.data["порядок"].append(name)
        return self

    def _feat_of(self, op_name):
        if op_name in self._op2feat:
            return self._op2feat[op_name]
        # чистовой — пара к черновой по той же фиче
        return self._op2feat.get(op_name.replace("Finish", "Rough", 1))

    @staticmethod
    def _laps(start, final, step):
        if start is None or final is None or not step:
            return None
        import math
        return max(1, int(math.ceil((start - final) / step - 1e-6)))

    # ── вывод ─────────────────────────────────────────────────────────────────

    def summary(self):
        f, t = len(self.data["фичи"]), len(self.data["переходы"])
        u = len(self.data["невыполнено"])
        return f"фич {f}, переходов {t}, невыполненных шагов {u}"

    def dump(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        return path

    @staticmethod
    def path_for(gcode_path):
        return os.path.splitext(gcode_path)[0] + "_plan.json"

    def ground(self, op_name, шаг, блок, правило, значение, вид="выбор"):
        """Обоснование решения: шаг алгоритма, блок базы, правило и число.

        Заводится ПО ИМЕНИ ОПЕРАЦИИ до того, как переход собран, — решение
        принимается раньше, чем существует объект операции.
        """
        self._ground.setdefault(op_name, []).append(
            {"шаг": шаг, "блок": блок, "правило": правило,
             "значение": значение, "вид": вид})


def load(path):
    """Входной план → (данные, {имя операции: переход}).

    Индекс по ИМЕНИ ОПЕРАЦИИ, а не по id фичи: исполнитель заново
    классифицирует геометрию и опознаёт запись плана по тому же
    детерминированному имени, которое сам и присвоит.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data, {t["id"]: t for t in data.get("переходы", []) if t.get("id")}
