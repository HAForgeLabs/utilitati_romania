from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import json
from pathlib import Path
import sys
import types

import aiohttp


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Nu pot încărca {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_ppc_client(integration_dir: Path):
    package = types.ModuleType("utilitati_romania")
    package.__path__ = [str(integration_dir)]
    sys.modules["utilitati_romania"] = package

    furnizori_dir = integration_dir / "furnizori"
    furnizori_pkg = types.ModuleType("utilitati_romania.furnizori")
    furnizori_pkg.__path__ = [str(furnizori_dir)]
    sys.modules["utilitati_romania.furnizori"] = furnizori_pkg

    load_module("utilitati_romania.exceptions", integration_dir / "exceptions.py")
    load_module("utilitati_romania.modele", integration_dir / "modele.py")
    load_module("utilitati_romania.furnizori.baza", furnizori_dir / "baza.py")
    ppc = load_module("utilitati_romania.furnizori.ppc", furnizori_dir / "ppc.py")
    return ppc.ClientFurnizorPpc


def consum_map(snapshot, account_id: str) -> dict:
    result = {}
    for item in snapshot.consumuri:
        if item.id_cont == account_id:
            result[item.cheie] = {
                "valoare": item.valoare,
                "unitate": item.unitate,
                "perioada": item.perioada,
            }
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Testează direct clientul PPC final din integrarea Utilități România."
    )
    parser.add_argument(
        "--integration-dir",
        default="utilitati_romania",
        help="Folderul integrării extrase. Implicit: .\\utilitati_romania",
    )
    args = parser.parse_args()

    integration_dir = Path(args.integration_dir).resolve()
    ppc_path = integration_dir / "furnizori" / "ppc.py"
    if not ppc_path.exists():
        print(f"Nu găsesc {ppc_path}")
        return 2

    ClientFurnizorPpc = load_ppc_client(integration_dir)

    username = input("Utilizator PPC: ").strip()
    password = getpass.getpass("Parola PPC: ")
    password_check = getpass.getpass("Repetă parola PPC: ")
    if password != password_check:
        print("Parolele nu coincid.")
        return 2

    async with aiohttp.ClientSession() as session:
        client = ClientFurnizorPpc(
            sesiune=session,
            utilizator=username,
            parola=password,
            optiuni={},
        )

        print("[PPC FINAL TEST] test_connection_start")
        unique_id = await client.async_testeaza_conexiunea()
        print(
            "[PPC FINAL TEST] test_connection_ok",
            json.dumps({"unique_id_prefix": unique_id[:6] + "…"}, ensure_ascii=False),
        )

        print("[PPC FINAL TEST] snapshot_start")
        snapshot = await client.async_obtine_instantaneu()

        summary = {
            "provider": snapshot.furnizor,
            "accounts": len(snapshot.conturi),
            "invoices": len(snapshot.facturi),
            "consumptions": len(snapshot.consumuri),
        }
        print("[PPC FINAL TEST] snapshot_ok", json.dumps(summary, ensure_ascii=False))

        for index, account in enumerate(snapshot.conturi, start=1):
            values = consum_map(snapshot, account.id_cont)
            raw = account.date_brute or {}
            account_summary = {
                "ordinal": index,
                "id_suffix": account.id_cont[-4:],
                "tip_serviciu": account.tip_serviciu,
                "tip_utilitate": account.tip_utilitate,
                "pod_present": bool(raw.get("pod")),
                "distribuitor_present": bool(raw.get("distribuitor")),
                "facturi_raw": len(raw.get("facturi") or []),
                "citiri_raw": len(raw.get("citiri") or []),
                "istoric_index_raw": len(raw.get("istoric_index") or []),
                "istoric_consum_raw": len(raw.get("istoric_consum") or []),
                "mapped": {
                    key: values.get(key)
                    for key in (
                        "sold_curent",
                        "de_plata",
                        "numar_facturi",
                        "numar_facturi_neachitate",
                        "valoare_ultima_factura",
                        "urmatoarea_scadenta",
                        "index_contor",
                        "serie_contor",
                        "data_ultimei_citiri",
                        "citire_permisa",
                        "perioada_citire",
                        "consum_lunar",
                        "consum_ultimele_12_luni",
                    )
                },
            }
            print(
                "[PPC FINAL TEST] account",
                json.dumps(account_summary, ensure_ascii=False),
            )

        invoice_preview = []
        for invoice in snapshot.facturi[:3]:
            invoice_preview.append(
                {
                    "id": invoice.id_factura,
                    "valoare": invoice.valoare,
                    "emitere": invoice.data_emitere.isoformat() if invoice.data_emitere else None,
                    "scadenta": invoice.data_scadenta.isoformat() if invoice.data_scadenta else None,
                    "stare": invoice.stare,
                    "tip_serviciu": invoice.tip_serviciu,
                }
            )
        print(
            "[PPC FINAL TEST] invoices_preview",
            json.dumps(invoice_preview, ensure_ascii=False),
        )

    print("[PPC FINAL TEST] result", json.dumps({"ok": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nTest anulat.")
        raise SystemExit(130)
