import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services import rclone_oauth, rclone_svc


class AutorespondTests(unittest.TestCase):
    def test_does_not_fill_blank_drive_id(self):
        option = {"Name": "drive_id", "Advanced": True, "Default": ""}
        self.assertIsNone(rclone_svc._should_autorespond(option, False))

    def test_does_not_fill_blank_drive_type(self):
        option = {"Name": "drive_type", "Advanced": True, "Default": ""}
        self.assertIsNone(rclone_svc._should_autorespond(option, False))

    def test_picks_onedrive_connection_type(self):
        option = {
            "Name": "config_type",
            "Exclusive": True,
            "Examples": [{"Value": "onedrive"}, {"Value": "sharepoint"}],
        }
        self.assertEqual(rclone_svc._should_autorespond(option, False), "onedrive")

    def test_advanced_mode_lets_user_pick_connection_type(self):
        option = {"Name": "config_type"}
        self.assertIsNone(rclone_svc._should_autorespond(option, True))

    def test_confirms_selected_drive(self):
        option = {"Name": "config_drive_ok", "Default": True}
        self.assertEqual(rclone_svc._should_autorespond(option, False), "true")

    def test_remote_name_from_fs(self):
        self.assertEqual(rclone_svc._remote_name_from_fs("onedrive:"), "onedrive")
        self.assertEqual(rclone_svc._remote_name_from_fs("onedrive:Backups"), "onedrive")
        self.assertIsNone(rclone_svc._remote_name_from_fs("/apps"))
        self.assertIsNone(rclone_svc._remote_name_from_fs(""))


class NeedsDriveTests(unittest.TestCase):
    def test_missing_ids(self):
        self.assertTrue(
            rclone_svc._needs_onedrive_drive({"type": "onedrive", "token": "{}"})
        )

    def test_complete(self):
        self.assertFalse(
            rclone_svc._needs_onedrive_drive(
                {"type": "onedrive", "drive_id": "abc", "drive_type": "business"}
            )
        )

    def test_other_backend(self):
        self.assertFalse(rclone_svc._needs_onedrive_drive({"type": "drive"}))


class MeDriveTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_default_drive(self):
        token = {"access_token": "tok", "refresh_token": "ref"}
        graph = {
            "id": "drive-1",
            "driveType": "business",
            "name": "Documents",
        }
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = graph
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        with patch("app.services.rclone_oauth.httpx.AsyncClient", return_value=client):
            info = await rclone_oauth.onedrive_me_drive(token)
        self.assertEqual(info["id"], "drive-1")
        self.assertEqual(info["driveType"], "business")
        self.assertEqual(info["name"], "Documents")

    async def test_refresh_on_401_then_succeeds(self):
        token = {"access_token": "old", "refresh_token": "ref"}
        unauthorized = Mock()
        unauthorized.status_code = 401
        unauthorized.json.return_value = {"error": {"message": "expired"}}
        ok = Mock()
        ok.status_code = 200
        ok.json.return_value = {"id": "drive-2", "driveType": "personal", "name": "Files"}
        refresh = Mock()
        refresh.status_code = 200
        refresh.json.return_value = {
            "access_token": "new",
            "refresh_token": "ref2",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        graph_client = AsyncMock()
        graph_client.get = AsyncMock(side_effect=[unauthorized, ok])
        graph_client.__aenter__.return_value = graph_client
        graph_client.__aexit__.return_value = None

        token_client = AsyncMock()
        token_client.post = AsyncMock(return_value=refresh)
        token_client.__aenter__.return_value = token_client
        token_client.__aexit__.return_value = None

        with patch(
            "app.services.rclone_oauth.httpx.AsyncClient",
            side_effect=[graph_client, token_client, graph_client],
        ):
            info = await rclone_oauth.onedrive_me_drive(token)
        self.assertEqual(info["id"], "drive-2")
        self.assertEqual(info["driveType"], "personal")
        refreshed = json.loads(info["token"])
        self.assertEqual(refreshed["access_token"], "new")


class EnsureDriveTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_ids_when_missing(self):
        conf = {"type": "onedrive", "token": '{"access_token":"tok"}'}
        info = {"id": "b!abc", "driveType": "business", "token": conf["token"]}
        with (
            patch.object(rclone_svc, "get_remote", new_callable=AsyncMock, return_value=conf),
            patch.object(
                rclone_oauth, "onedrive_me_drive", new_callable=AsyncMock, return_value=info
            ),
            patch.object(rclone_svc, "update_remote", new_callable=AsyncMock) as update,
        ):
            saved = await rclone_svc.ensure_onedrive_drive_ids("onedrive")
        update.assert_awaited_once_with(
            "onedrive", {"drive_id": "b!abc", "drive_type": "business"}
        )
        self.assertEqual(saved["id"], "b!abc")

    async def test_skips_complete_remote(self):
        conf = {"type": "onedrive", "drive_id": "x", "drive_type": "business"}
        with (
            patch.object(rclone_svc, "get_remote", new_callable=AsyncMock, return_value=conf),
            patch.object(rclone_oauth, "onedrive_me_drive", new_callable=AsyncMock) as lookup,
            patch.object(rclone_svc, "update_remote", new_callable=AsyncMock) as update,
        ):
            self.assertIsNone(await rclone_svc.ensure_onedrive_drive_ids("onedrive"))
        lookup.assert_not_called()
        update.assert_not_called()

    async def test_list_repairs_missing_drive_then_retries(self):
        err = RuntimeError("unable to get drive_id and drive_type - please run rclone config")
        listed = {"list": [{"Name": "Documents"}]}
        with (
            patch.object(
                rclone_svc, "rc_call", new_callable=AsyncMock, side_effect=[err, listed]
            ),
            patch.object(
                rclone_svc, "ensure_onedrive_drive_ids", new_callable=AsyncMock, return_value={"id": "x"}
            ) as ensure,
        ):
            result = await rclone_svc.operations_list("onedrive:", "")
        ensure.assert_awaited_once_with("onedrive")
        self.assertEqual(result, listed)

    async def test_wizard_fills_ids_when_rclone_finishes_without_them(self):
        with (
            patch.object(
                rclone_svc,
                "_config_create",
                new_callable=AsyncMock,
                return_value={"State": "", "Option": None, "Error": ""},
            ),
            patch.object(
                rclone_svc, "ensure_onedrive_drive_ids", new_callable=AsyncMock, return_value={"id": "x"}
            ) as ensure,
        ):
            payload = await rclone_svc.wizard_step("onedrive", "onedrive")
        ensure.assert_awaited_once_with("onedrive")
        self.assertTrue(payload["done"])
        self.assertEqual(payload["error"], "")
