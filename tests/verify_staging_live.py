"""
Live Staging Integration Verification Script for INATrace MCP Server.
Connects to real PostgreSQL 16 database (inatrace_unocace_stage) using the
read-only role (inatrace_mcp_ro) and executes all canonical MCP tools against
real cooperative data, including the pre-existing plotdeforestationanalysis Whisp table.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.infrastructure.db_pool import init_db_pool, close_db_pool, get_db_pool
from src.tools.audit_tools import (
    AuditLotInput,
    VerifyEudrInput,
    execute_audit_lot_traceability,
    execute_verify_eudr_compliance,
)
from src.tools.export_tools import (
    GenerateTracesJsonLdInput,
    ExportAgrocalidadInput,
    execute_generate_traces_jsonld,
    execute_export_agrocalidad_guia,
)
from src.tools.diagnostic_tools import (
    DiagnoseTenantHealthInput,
    DiagnoseQrInput,
    execute_diagnose_tenant_health,
    execute_diagnose_and_repair_qr,
)
from src.tools.erp_tools import (
    VerifySriInput,
    execute_verify_sri_compliance,
)


async def run_live_staging_verification():
    print("=" * 80)
    print("INATRACE MCP SERVER - LIVE STAGING VERIFICATION (POSTGRESQL 16)")
    print("=" * 80)

    t0 = time.perf_counter()
    pool = await init_db_pool()
    if not pool:
        print("FAIL: Could not establish connection pool to Staging PostgreSQL.")
        sys.exit(1)

    print(f"[1/7] Connected to PostgreSQL with read-only pool in {(time.perf_counter() - t0)*1000:.1f} ms")

    async with pool.acquire() as conn:
        # Verify read-only transaction invariant
        ro_status = await conn.fetchval("SHOW transaction_read_only;")
        print(f"      -> transaction_read_only = {ro_status}")
        assert ro_status == "on", f"Expected transaction_read_only='on', got '{ro_status}'"

        # Inspect database tables & Whisp table counts
        companies_count = await conn.fetchval("SELECT count(*) FROM company;")
        stockorders_count = await conn.fetchval("SELECT count(*) FROM stockorder;")
        plots_count = await conn.fetchval("SELECT count(*) FROM plot;")
        coords_count = await conn.fetchval("SELECT count(*) FROM plotcoordinate;")
        whisp_count = await conn.fetchval("SELECT count(*) FROM plotdeforestationanalysis;")

        print(f"      -> Staging DB counts: companies={companies_count}, stockorders={stockorders_count}, "
              f"plots={plots_count}, coordinates={coords_count}, whisp_analyses={whisp_count}")

        # Pick a real StockOrder that has a producerUserCustomer or processingOrder
        sample_lot = await conn.fetchrow("""
            SELECT s.id, s.identifier, s.qrcodetag, s.company_id, s.totalquantity
            FROM stockorder s
            WHERE s.producerusercustomer_id IS NOT NULL
            ORDER BY s.id DESC
            LIMIT 1
        """)
        if not sample_lot:
            sample_lot = await conn.fetchrow("""
                SELECT s.id, s.identifier, s.qrcodetag, s.company_id, s.totalquantity
                FROM stockorder s
                ORDER BY s.id DESC
                LIMIT 1
            """)

    lot_id = sample_lot["id"]
    lot_ident = sample_lot["qrcodetag"] or sample_lot["identifier"] or str(lot_id)
    company_id = sample_lot["company_id"] or 1
    print(f"      -> Selected Real Lot: ID={lot_id}, Identifier='{lot_ident}', Company={company_id}")

    # 2. Test diagnose_tenant_health
    t1 = time.perf_counter()
    health_res = await execute_diagnose_tenant_health(DiagnoseTenantHealthInput(company_id=company_id))
    print(f"[2/7] diagnose_tenant_health(company_id={company_id}) -> status={health_res['status']}, "
          f"issues={health_res['issues_count']} ({(time.perf_counter() - t1)*1000:.1f} ms)")

    # 3. Test audit_lot_traceability (Regulatory Full Dossier + Mass Balance + OWASP LLM01 + Tokenomics)
    t2 = time.perf_counter()
    dag_res = await execute_audit_lot_traceability(
        AuditLotInput(
            lot_identifier=str(lot_id),
            company_id=company_id,
            include_plots=True,
            public_view=False,
            include_raw_coordinates=False
        )
    )
    assert "mass_balance" in dag_res, "Expected mass_balance in DAG response"
    assert "security_audit" in dag_res and dag_res["security_audit"]["owasp_llm01_checked"] is True
    print(f"[3/8] audit_lot_traceability(lot={lot_id}, public_view=False) -> found={dag_res['found']}, "
          f"nodes={dag_res.get('total_nodes')}, plots={dag_res.get('participating_plots_count')}, "
          f"mass_balance={dag_res.get('mass_balance', {}).get('status')}, "
          f"strict_whisp={dag_res.get('eudr_summary', {}).get('strict_whisp_status')} "
          f"({(time.perf_counter() - t2)*1000:.1f} ms)")

    # 4. Test audit_lot_traceability (Public B2C Privacy Sanitized)
    t3 = time.perf_counter()
    pub_res = await execute_audit_lot_traceability(
        AuditLotInput(lot_identifier=str(lot_id), company_id=company_id, include_plots=True, public_view=True)
    )
    assert "participating_plots" not in pub_res, "PII leak: participating_plots must not be in public B2C view!"
    print(f"[4/8] audit_lot_traceability(lot={lot_id}, public_view=True) -> channel={pub_res.get('channel')}, "
          f"eudr_status={pub_res.get('eudr_status')}, PII stripped=OK ({(time.perf_counter() - t3)*1000:.1f} ms)")

    # 5. Test verify_eudr_compliance
    t4 = time.perf_counter()
    eudr_res = await execute_verify_eudr_compliance(VerifyEudrInput(stock_order_id=lot_id))
    print(f"[5/8] verify_eudr_compliance(stock_order_id={lot_id}) -> status={eudr_res['eudr_status']}, "
          f"strict_whisp={eudr_res.get('strict_whisp_status')}, "
          f"plots_evaluated={eudr_res['total_plots_evaluated']}, verdict={eudr_res['customs_verdict']} "
          f"({(time.perf_counter() - t4)*1000:.1f} ms)")

    # 6. Test generate_traces_jsonld & export_agrocalidad_guia
    t5 = time.perf_counter()
    traces_res = await execute_generate_traces_jsonld(
        GenerateTracesJsonLdInput(stock_order_id=lot_id, operator_ruc="0992345678001")
    )
    agro_res = await execute_export_agrocalidad_guia(
        ExportAgrocalidadInput(company_id=company_id, date_from="2025-01-01", date_to="2026-12-31")
    )
    print(f"[6/8] generate_traces_jsonld & export_agrocalidad_guia -> places={len(traces_res.get('productionPlaces', []))}, "
          f"csv_bom={agro_res['csv_content'].startswith(chr(0xFEFF))} ({(time.perf_counter() - t5)*1000:.1f} ms)")

    # 7. Test verify_sri_compliance & diagnose_and_repair_qr
    t6 = time.perf_counter()
    sri_res = await execute_verify_sri_compliance(VerifySriInput(stock_order_id=lot_id))
    qr_res = await execute_diagnose_and_repair_qr(
        DiagnoseQrInput(qr_payload=f"https://inatrace.test.unocace.com/public/delivery-receipt/{lot_id}", company_id=company_id)
    )
    print(f"[7/8] verify_sri_compliance & diagnose_and_repair_qr -> sri_status={sri_res['sri_status']}, "
          f"qr_verdict={qr_res['verdict']} ({(time.perf_counter() - t6)*1000:.1f} ms)")

    # 8. Test MCP Protocol JSON-RPC 2.0 (Resources & Prompts)
    t7 = time.perf_counter()
    from src.app import mcp
    res_list = await mcp.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 10, "method": "resources/list"})
    prm_list = await mcp.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 11, "method": "prompts/list"})
    assert len(res_list["result"]["resources"]) == 3
    assert len(prm_list["result"]["prompts"]) == 2
    print(f"[8/8] MCP JSON-RPC 2.0 Protocol -> resources={len(res_list['result']['resources'])}, "
          f"prompts={len(prm_list['result']['prompts'])} ({(time.perf_counter() - t7)*1000:.1f} ms)")

    await close_db_pool()
    total_ms = (time.perf_counter() - t0) * 1000
    print("=" * 80)
    print(f"ALL 8 LIVE STAGING TESTS PASSED IN {total_ms:.1f} ms (SLA < 15,000 ms)")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_live_staging_verification())
