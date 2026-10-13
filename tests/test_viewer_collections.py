from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.main import app
from app.schemas import FolderInfo


class ViewerSourceEventsTest(unittest.TestCase):
    def test_unchanged_sources_emit_heartbeats_to_release_departed_clients(self) -> None:
        with patch("app.main.db.get_folders", return_value=[]), patch("app.main.time.sleep"):
            response = app.test_client().get("/api/folders/events", buffered=False)
            try:
                self.assertTrue(next(response.response).startswith(b"data: "))
                self.assertEqual(next(response.response), b": keep-alive\n\n")
            finally:
                response.close()

    def test_source_events_include_mixed_media_totals_and_progress(self) -> None:
        folder = FolderInfo(
            id=1, path="F:/fixtures/media", name="Mixed source",
            image_count=2, video_count=2, asset_count=4,
            processed_count=2, processed_asset_count=3, status="processing",
        )
        with patch("app.main.db.get_folders", return_value=[folder]):
            response = app.test_client().get("/api/folders/events", buffered=False)
            try:
                event = next(response.response).decode("utf-8")
                state = json.loads(event.removeprefix("data: ").strip())["1"]
                self.assertEqual(state["image_count"], 2)
                self.assertEqual(state["video_count"], 2)
                self.assertEqual(state["asset_count"], 4)
                self.assertEqual(state["processed_asset_count"], 3)
            finally:
                response.close()
