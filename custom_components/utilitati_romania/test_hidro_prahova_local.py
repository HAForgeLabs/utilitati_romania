#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
import getpass
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
from typing import Any


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Nu pot incarca modulul {name} din {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_hidro_provider(integration_dir: Path):
    integration_dir = integration_dir.resolve()
    provider_dir = integration_dir / "furnizori"
    required = [
        integration_dir / "exceptions.py",
        integration_dir / "modele.py",
        provider_dir / "baza.py",
        provider_dir / "hidro_prahova.py",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("Lipsesc fisierele necesare:\n- " + "\n- ".join(missing))

    root_name = "ur_local_test"
    root_pkg = types.ModuleType(root_name)
    root_pkg.__path__ = [str(integration_dir)]
    sys.modules[root_name] = root_pkg

    providers_name = f"{root_name}.furnizori"
    providers_pkg = types.ModuleType(providers_name)
    providers_pkg.__path__ = [str(provider_dir)]
    sys.modules[providers_name] = providers_pkg

    _load_module(f"{root_name}.exceptions", integration_dir / "exceptions.py")
    _load_module(f"{root_name}.modele", integration_dir / "modele.py")
    _load_module(f"{providers_name}.baza", provider_dir / "baza.py")
    return _load_module(f"{providers_name}.hidro_prahova", provider_dir / "hidro_prahova.py")


def _candidate_integration_dirs() -> list[Path]:
    here = Path.cwd()
    return [
        Path("/config/custom_components/utilitati_romania"),
        here / "custom_components" / "utilitati_romania",
        here / "utilitati_romania",
        here,
    ]


def find_integration_dir(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if (p / "furnizori" / "hidro_prahova.py").exists():
            return p
        raise FileNotFoundError(f"Nu gasesc furnizori/hidro_prahova.py in {p}")

    for p in _candidate_integration_dirs():
        if (p / "furnizori" / "hidro_prahova.py").exists():
            return p.resolve()
    raise FileNotFoundError(
        "Nu am gasit integrarea. Ruleaza cu --integration-dir /cale/catre/custom_components/utilitati_romania"
    )


def _safe_factura(item: dict[str, Any]) -> dict[str, Any]:
    # Pastram identificatorii facturii fiind esentiali pentru reconcilierea celor doua surse,
    # dar eliminam linkurile complete care pot contine parametri inutili.
    keys = (
        "id_factura",
        "numar_factura",
        "data_emitere",
        "valoare",
        "valoare_fara_tva",
        "tva",
        "incasat",
        "restant",
        "stare",
        "gestiune",
        "sursa",
        "id_cont",
    )
    return {k: _json_value(item.get(k)) for k in keys if k in item}


def _table_rows(module, html: str, limit: int = 15) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html or "", flags=re.I | re.S):
        row_html = row_match.group(1) or ""
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        cleaned = [module._curata_text(cell) for cell in cells]
        cleaned = [c for c in cleaned if c]
        if len(cleaned) < 2:
            continue
        joined = " | ".join(cleaned).lower()
        # Pastram doar randuri care par relevante pentru facturi / plati.
        if not (
            re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", joined)
            or "rest plata" in joined
            or "restant" in joined
            or "incasat" in joined
            or "factura" in joined
        ):
            continue
        rows.append(cleaned)
    return rows[:limit]


def _match_sources(facturi_emise: list[dict[str, Any]], facturi_fisa: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    used_fisa: set[int] = set()
    for e in facturi_emise:
        e_date = _json_value(e.get("data_emitere"))
        e_value = e.get("valoare")
        best_idx = None
        for idx, f in enumerate(facturi_fisa):
            if idx in used_fisa:
                continue
            f_date = _json_value(f.get("data_emitere"))
            f_value = f.get("valoare")
            same_date = bool(e_date and f_date and e_date == f_date)
            same_value = (
                e_value is not None
                and f_value is not None
                and abs(float(e_value) - float(f_value)) < 0.01
            )
            same_number = str(e.get("numar_factura") or "").strip() == str(f.get("numar_factura") or "").strip()
            if same_number or (same_date and same_value):
                best_idx = idx
                break
        if best_idx is None:
            matches.append({"facturi_emise": _safe_factura(e), "fisa_financiara": None})
        else:
            used_fisa.add(best_idx)
            matches.append({
                "facturi_emise": _safe_factura(e),
                "fisa_financiara": _safe_factura(facturi_fisa[best_idx]),
            })
    for idx, f in enumerate(facturi_fisa):
        if idx not in used_fisa:
            matches.append({"facturi_emise": None, "fisa_financiara": _safe_factura(f)})
    return matches


async def run_test(integration_dir: Path, username: str, password: str, output: Path | None) -> Path:
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError("Lipseste pachetul aiohttp in mediul Python din care rulezi testerul.") from exc

    hp = load_hidro_provider(integration_dir)

    timeout = aiohttp.ClientTimeout(total=45)
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        api = hp.ClientApiHidroPrahova(session, username, password)
        print("[1/3] Autentificare Hidro Prahova...")
        await api.async_login()
        print("      OK")

        print("[2/3] Citire date prin clientul actual al integrarii...")
        data = await api.async_get_all_data()
        print("      OK")

    conturi_out: list[dict[str, Any]] = []
    surse: dict[str, Any] = {}

    for cont in data.get("conturi", []):
        id_cont = str(cont.get("id_cont") or "")
        facturi_html = data.get("pagini", {}).get(f"facturi_{id_cont}", "")
        fisa_html = data.get("pagini", {}).get(f"fisa_{id_cont}", "")

        facturi_emise = hp._extrage_facturi_emise(facturi_html, id_cont) if facturi_html else []
        facturi_fisa = hp._extrage_facturi_fisa(fisa_html, id_cont) if fisa_html else []
        rezumat = hp._extrage_rezumat_fisa(fisa_html) if fisa_html else {}

        conturi_out.append({
            "id_cont": id_cont,
            "sold_final": _json_value(cont.get("sold_final")),
            "sold_curent": _json_value(cont.get("sold_curent")),
            "total_neachitat": _json_value(cont.get("total_neachitat")),
            "de_plata": _json_value(cont.get("de_plata")),
            "numar_facturi_selectate_de_integrare": _json_value(cont.get("numar_facturi")),
            "numar_facturi_neachitate_selectate": _json_value(cont.get("numar_facturi_neachitate")),
            "valoare_ultima_factura_selectata": _json_value(cont.get("valoare_ultima_factura")),
            "id_ultima_factura_selectata": _json_value(cont.get("id_ultima_factura")),
            "data_ultima_factura_selectata": _json_value(cont.get("data_ultima_factura")),
        })

        surse[id_cont] = {
            "rezumat_fisa": _json_value({
                k: v for k, v in rezumat.items() if k != "nume_client"
            }),
            "numar_facturi_emise": len(facturi_emise),
            "numar_facturi_fisa": len(facturi_fisa),
            "facturi_emise": [_safe_factura(x) for x in facturi_emise],
            "facturi_fisa": [_safe_factura(x) for x in facturi_fisa],
            "reconciliere_dupa_numar_sau_data_valoare": _match_sources(facturi_emise, facturi_fisa),
            "randuri_relevante_facturi_emise": _table_rows(hp, facturi_html),
            "randuri_relevante_fisa_financiara": _table_rows(hp, fisa_html),
        }

    selected = [_safe_factura(x) for x in data.get("facturi", [])]
    result = {
        "test": "Hidro Prahova - comparatie surse facturi",
        "generated_at": datetime.now().astimezone().isoformat(),
        "integration_dir": str(integration_dir),
        "provider_file": str(integration_dir / "furnizori" / "hidro_prahova.py"),
        "credentials_included": False,
        "observatie": (
            "Fisierul nu contine parola. Sunt pastrati identificatorii contului si facturilor deoarece sunt necesari "
            "pentru compararea surselor Facturi emise si Fisa financiara."
        ),
        "conturi": conturi_out,
        "facturi_selectate_de_logica_actuala": selected,
        "surse": surse,
    }

    if output is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = Path.cwd() / f"hidro_prahova_test_{stamp}.json"
    output = output.expanduser().resolve()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("[3/3] Raport generat.")
    for id_cont, src in surse.items():
        fe = src["facturi_emise"]
        ff = src["facturi_fisa"]
        print(f"\nCont {id_cont}:")
        print(f"  Facturi emise: {len(fe)} | Fisa financiara: {len(ff)}")
        if fe:
            x = fe[0]
            print(
                "  Ultima din Facturi emise: "
                f"nr={x.get('numar_factura')} data={x.get('data_emitere')} "
                f"valoare={x.get('valoare')} restant={x.get('restant')} stare={x.get('stare')}"
            )
        if ff:
            x = ff[0]
            print(
                "  Ultima din Fisa financiara: "
                f"nr={x.get('numar_factura')} data={x.get('data_emitere')} "
                f"valoare={x.get('valoare')} incasat={x.get('incasat')} "
                f"restant={x.get('restant')} stare={x.get('stare')}"
            )
        c = next((c for c in conturi_out if c.get("id_cont") == id_cont), {})
        print(f"  De plata / total neachitat: {c.get('de_plata')} / {c.get('total_neachitat')}")

    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tester local Hidro Prahova: compara Facturi emise cu Fisa financiara fara restart Home Assistant."
    )
    parser.add_argument(
        "--integration-dir",
        help="Calea catre custom_components/utilitati_romania. Implicit incearca /config/custom_components/utilitati_romania.",
    )
    parser.add_argument("--user", help="Utilizator Hidro Prahova. Daca lipseste, este cerut interactiv.")
    parser.add_argument("--output", help="Calea fisierului JSON rezultat.")
    args = parser.parse_args()

    try:
        integration_dir = find_integration_dir(args.integration_dir)
        print(f"Integrare folosita: {integration_dir}")
        username = (args.user or input("Utilizator Hidro Prahova: ")).strip()
        if not username:
            raise ValueError("Utilizatorul nu poate fi gol")
        password = getpass.getpass("Parola Hidro Prahova (nu va fi salvata): ")
        if not password:
            raise ValueError("Parola nu poate fi goala")
        output = Path(args.output) if args.output else None
        report = asyncio.run(run_test(integration_dir, username, password, output))
        print(f"\nTrimite-mi acest fisier: {report}")
        return 0
    except KeyboardInterrupt:
        print("\nTest anulat.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nEROARE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
