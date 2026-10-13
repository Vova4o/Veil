from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.comfyui.workflow_errors import normalize_comfyui_error
from app.comfyui.workflow_execution import WorkflowExecutionService
from app.comfyui.workflow_store import WorkflowStore
from app.main import app

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z1XcAAAAASUVORK5CYII="
)


class FakeCompletedClient:
    def get_job(self, prompt_id):
        return {
            "status": "completed",
            "workflow": {"prompt": {"1": {"class_type": "SaveImage", "inputs": {}}}},
            "outputs": {
                "7": {
                    "images": [{"filename": "result.png", "subfolder": "", "type": "output"}],
                }
            },
        }

    def download_output(self, output):
        return PNG_1X1


class FakeFailedClient:
    def get_job(self, prompt_id):
        return {
            "status": "failed",
            "execution_error": {
                "node_id": "4",
                "node_type": "EmptyLatentImage",
                "exception_message": "invalid width",
                "exception_type": "invalid_input_type",
                "input_name": "width",
            },
        }


class WorkflowExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.get_db_path()
        database.set_db_path(Path(self.temp_dir.name) / "cmv.sqlite3")
        database.init_db()

    def tearDown(self) -> None:
        database.set_db_path(self.old_db_path)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_completed_output_is_imported_into_library(self) -> None:
        store = WorkflowStore()
        source_asset_id, _ = database.insert_upload_asset(
            "remix-parent.png",
            PNG_1X1,
            media_type="image",
            has_generation_metadata=False,
        )
        draft = store.create_draft(
            template_id="krea2_moody_v40",
            template_version="1",
            values={"prompt": "a blue landscape"},
            resource_selections={"checkpoint": "models/base-xl.safetensors"},
            source_asset_id=source_asset_id,
        )
        run = store.create_run(draft_id=draft.id, prompt_id="prompt-1", client_id="client-1")

        completed = WorkflowExecutionService(store=store, client=FakeCompletedClient()).refresh(run.id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(completed.output_asset_ids), 1)
        detail = database.get_asset_detail(completed.output_asset_ids[0])
        self.assertIsNotNone(detail)
        self.assertEqual(detail.media_type, "image")
        self.assertEqual(detail.embedded_metadata["generation"]["template_id"], "krea2_moody_v40")
        conn = database.get_conn()
        try:
            lineage = conn.execute(
                "SELECT derived_from_asset_id FROM images WHERE id=?",
                (completed.output_asset_ids[0],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(lineage["derived_from_asset_id"], source_asset_id)

    def test_failed_run_persists_normalized_field_diagnostic_and_raw_error(self) -> None:
        store = WorkflowStore()
        draft = store.create_draft(
            template_id="krea2_moody_v40",
            template_version="1",
            values={"prompt": "a blue landscape"},
            resource_selections={"checkpoint": "models/base-xl.safetensors"},
        )
        run = store.create_run(draft_id=draft.id, prompt_id="prompt-failed", client_id="client-1")

        failed = WorkflowExecutionService(store=store, client=FakeFailedClient()).refresh(run.id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["category"], "invalid_input")
        self.assertEqual(failed.error["node_id"], "4")
        self.assertEqual(failed.error["input_name"], "width")
        self.assertEqual(failed.error["raw"]["exception_message"], "invalid width")

    def test_create_results_exclude_deleted_assets_and_keep_run_history(self) -> None:
        store = WorkflowStore()
        draft = store.create_draft(
            template_id="krea2_moody_v40", template_version="1",
            values={"prompt": "a blue landscape"}, resource_selections={},
        )
        run = store.create_run(draft_id=draft.id, prompt_id="deleted-output", client_id="test")
        asset_id, _ = database.insert_upload_asset("result.png", PNG_1X1, media_type="image")
        store.update_run(run.id, status="completed", output_asset_ids=[asset_id])
        self.assertTrue(database.delete_image(asset_id))
        with patch("app.comfyui.simple_routes._backend") as backend:
            backend.return_value.monitor.return_value = None
            backend.return_value.run_details.return_value = None
            response = app.test_client().get(f"/api/simple/runs/{run.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["outputs"], [])
        self.assertEqual(store.get_run(run.id).output_asset_ids, [asset_id])

    def test_out_of_memory_diagnostic_recommends_batch_and_resolution_fields(self) -> None:
        diagnostic = normalize_comfyui_error(
            {
                "node_id": "5",
                "node_type": "KSampler",
                "exception_message": "CUDA out of memory",
                "exception_type": "RuntimeError",
            },
        )

        self.assertEqual(diagnostic["category"], "out_of_memory")
        self.assertIn("Reduce", diagnostic["suggested_action"])

    def test_runtime_error_categories_distinguish_incompatibility_cancellation_and_failure(self) -> None:
        cases = [
            (
                {"type": "return_type_mismatch", "message": "Return type mismatch between linked nodes"},
                "failed",
                "workflow_incompatible",
            ),
            ({"message": "Interrupted by user"}, "cancelled", "cancelled"),
            ({"exception_message": "Custom node crashed"}, "failed", "execution_failure"),
        ]

        for raw, status, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = normalize_comfyui_error(raw, status=status)
                self.assertEqual(diagnostic["category"], expected)
                self.assertEqual(diagnostic["raw"], raw)

    def test_preview_route_fallback_to_original_when_preview_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_db = database.get_db_path()
            try:
                database.set_db_path(Path(temp_dir) / "test.db")
                database.init_db()
                asset_id, _ = database.insert_upload_asset(
                    "test.png",
                    PNG_1X1,
                    media_type="image",
                    format_name="PNG",
                    width=1,
                    height=1,
                )
                with app.test_client() as client:
                    with patch("app.main.get_or_create_preview", side_effect=RuntimeError("Preview busy or failed")):
                        res = client.get(f"/api/preview/{asset_id}")
                        self.assertEqual(res.status_code, 200)
                        self.assertEqual(res.mimetype, "image/png")
                        self.assertEqual(res.data, PNG_1X1)
            finally:
                database.set_db_path(old_db)
