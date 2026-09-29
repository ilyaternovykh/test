# -*- coding: utf-8 -*-
"""Реестр коллекторов ilo-exporter."""

from __future__ import annotations

from . import redfish, ribcl, web

REGISTRY = {
    "web": web.collect,          # доступность web iLO (есть на всех поколениях)
    "redfish": redfish.collect,  # метрики по Redfish API
    "ribcl": ribcl.collect,      # IML/EL через RIBCL (задел, включается per-target)
}


def resolve(names: list[str], target) -> dict:
    """Возвращает {имя: callable} для данного target.

    Правила:
      * неизвестное имя коллектора игнорируется;
      * 'ribcl' добавляется автоматически, если у target ribcl_enabled=true.
    """
    out = {}
    for name in names or []:
        key = str(name).strip().lower()
        if key in REGISTRY:
            out[key] = REGISTRY[key]
    if getattr(target, "ribcl_enabled", False) and "ribcl" not in out:
        out["ribcl"] = REGISTRY["ribcl"]
    return out
