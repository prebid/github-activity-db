"""Tests for sync CLI commands."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from github_activity_db.cli.app import app
from github_activity_db.db.models import PRState

runner = CliRunner()


@pytest.fixture
def mock_ingestion_result():
    """Create a mock successful ingestion result."""
    return {
        "success": True,
        "created": True,
        "updated": False,
        "skipped_frozen": False,
        "skipped_unchanged": False,
        "action": "created",
        "pr_id": 1,
        "pr_number": 4663,
        "title": "Test PR Title",
        "state": PRState.OPEN.value,
        "error": None,
    }


@pytest.fixture
def mock_service(mock_ingestion_result):
    """Create a mock PRIngestionService."""
    service = MagicMock()
    mock_result = MagicMock()
    mock_result.to_dict.return_value = mock_ingestion_result
    service.ingest_pr = AsyncMock(return_value=mock_result)
    return service


class TestSyncPRCommand:
    """Tests for the 'sync pr' command."""

    def test_command_exists(self):
        """Verify sync pr command is registered."""
        result = runner.invoke(app, ["sync", "pr", "--help"])
        assert result.exit_code == 0
        assert "Sync a single PR" in result.stdout

    def test_requires_repo_argument(self):
        """Command requires repository argument."""
        result = runner.invoke(app, ["sync", "pr"])
        assert result.exit_code != 0
        # Error may be in stdout or output depending on Typer version
        assert "Missing argument" in result.output or "REPO" in result.output

    def test_requires_pr_number_argument(self):
        """Command requires PR number argument."""
        result = runner.invoke(app, ["sync", "pr", "owner/repo"])
        assert result.exit_code != 0
        # Error may be in stdout or output depending on Typer version
        assert "Missing argument" in result.output or "PR_NUMBER" in result.output

    def test_invalid_repo_format_rejected(self):
        """Repository must be in owner/name format."""
        result = runner.invoke(app, ["sync", "pr", "invalid-repo", "123"])
        assert result.exit_code == 1
        assert "owner/name format" in result.stdout


class TestSyncPRFlags:
    """Tests for sync pr command flags."""

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_format_json_outputs_json(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """--format json outputs valid JSON."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(
            app, ["sync", "pr", "owner/repo", "123", "--format", "json"]
        )

        assert result.exit_code == 0
        # Should be valid JSON
        output = json.loads(result.stdout)
        assert output["success"] is True
        assert output["action"] == "created"

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_quiet_suppresses_output_on_success(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """--quiet suppresses output on successful sync."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123", "--quiet"])

        assert result.exit_code == 0
        # Output should be empty (or minimal whitespace)
        assert result.stdout.strip() == ""

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_verbose_shows_extra_info(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """--verbose shows additional PR details."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123", "--verbose"])

        assert result.exit_code == 0
        assert "State:" in result.stdout
        assert "ID:" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_dry_run_shows_prefix(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """--dry-run shows (dry-run) prefix in output."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123", "--dry-run"])

        assert result.exit_code == 0
        assert "dry-run" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_dry_run_passes_flag_to_service(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """--dry-run flag is passed to ingestion service."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        runner.invoke(app, ["sync", "pr", "owner/repo", "123", "--dry-run"])

        # Verify dry_run=True was passed to ingest_pr
        mock_service_instance.ingest_pr.assert_called_once_with(
            "owner", "repo", 123, dry_run=True
        )


class TestSyncPRErrorHandling:
    """Tests for error handling in sync pr command."""

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_error_result_shows_error_and_exits_1(
        self, mock_service_class, mock_get_session, mock_client
    ):
        """Error in result shows error message and exits with code 1."""
        # Setup mocks with error result
        error_result = {
            "success": False,
            "created": False,
            "updated": False,
            "skipped_frozen": False,
            "skipped_unchanged": False,
            "action": "error",
            "pr_id": None,
            "pr_number": 123,
            "title": None,
            "state": None,
            "error": "PR not found",
        }
        mock_result = MagicMock()
        mock_result.to_dict.return_value = error_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123"])

        assert result.exit_code == 1
        assert "Error" in result.stdout
        assert "PR not found" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    def test_exception_shows_error_and_exits_1(self, mock_get_session, mock_client):
        """Exception during sync shows error and exits with code 1."""
        # Setup mock to raise exception
        mock_client.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("Connection failed")
        )

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123"])

        assert result.exit_code == 1
        assert "Error" in result.stdout


class TestSyncPRShortFlags:
    """Tests for short flag aliases."""

    def test_help_shows_short_flags(self):
        """Help text shows short flag aliases."""
        result = runner.invoke(app, ["sync", "pr", "--help"])
        assert "-v" in result.stdout  # --verbose
        assert "-q" in result.stdout  # --quiet
        assert "-f" in result.stdout  # --format

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_short_format_flag_works(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """-f json works same as --format json."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_pr = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = runner.invoke(app, ["sync", "pr", "owner/repo", "123", "-f", "json"])

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["success"] is True


# =============================================================================
# Tests for 'sync repo' command (Bulk Ingestion - Phase 1.7)
# =============================================================================


@pytest.fixture
def mock_bulk_ingestion_result():
    """Create a mock successful bulk ingestion result."""
    return {
        "total_discovered": 10,
        "created": 5,
        "updated": 2,
        "skipped_frozen": 2,
        "skipped_unchanged": 1,
        "failed": 0,
        "failed_prs": [],
        "duration_seconds": 15.5,
        "success_rate": 100.0,
    }


class TestSyncRepoCommand:
    """Tests for the 'sync repo' command."""

    def test_command_exists(self):
        """Verify sync repo command is registered."""
        result = runner.invoke(app, ["sync", "repo", "--help"])
        assert result.exit_code == 0
        assert "Sync all PRs" in result.stdout

    def test_requires_repo_argument(self):
        """Command requires repository argument."""
        result = runner.invoke(app, ["sync", "repo"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "REPO" in result.output

    def test_invalid_repo_format_rejected(self):
        """Repository must be in owner/name format."""
        result = runner.invoke(app, ["sync", "repo", "invalid-repo"])
        assert result.exit_code == 1
        assert "owner/name format" in result.stdout

    def test_invalid_state_rejected(self):
        """Invalid state value is rejected."""
        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--state", "invalid"])
        assert result.exit_code == 1
        assert "Invalid state" in result.stdout

    def test_invalid_since_date_rejected(self):
        """Invalid date format is rejected."""
        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--since", "not-a-date"])
        assert result.exit_code == 1
        assert "Invalid date" in result.stdout


class TestSyncRepoFlags:
    """Tests for sync repo command flags."""

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_format_json_outputs_json(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """--format json outputs valid JSON."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(
            app, ["sync", "repo", "owner/repo", "--format", "json"]
        )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["total_discovered"] == 10
        assert output["created"] == 5

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_quiet_suppresses_progress_output(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """--quiet suppresses progress output."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--quiet"])

        assert result.exit_code == 0
        # Should not show "Syncing PRs" progress message
        assert "Syncing PRs" not in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_dry_run_shows_prefix(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """--dry-run shows (dry-run) prefix in output."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--dry-run"])

        assert result.exit_code == 0
        assert "dry-run" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_since_date_parsed_correctly(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """--since date is parsed and passed to service."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(
            app, ["sync", "repo", "owner/repo", "--since", "2024-10-01"]
        )

        assert result.exit_code == 0
        # Verify the since date was shown in the progress output
        assert "2024-10-01" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_max_prs_passed_to_config(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """--max value is passed to BulkIngestionConfig."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--max", "10"])

        assert result.exit_code == 0
        # Verify the max was shown in the progress output
        assert "10" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_verbose_shows_failed_prs(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
    ):
        """--verbose shows failed PRs when there are failures."""
        # Setup mocks with failures
        result_with_failures = {
            "total_discovered": 10,
            "created": 5,
            "updated": 2,
            "skipped_frozen": 0,
            "skipped_unchanged": 0,
            "failed": 3,
            "failed_prs": [
                {"pr_number": 100, "error": "API error"},
                {"pr_number": 101, "error": "Timeout"},
                {"pr_number": 102, "error": "Not found"},
            ],
            "duration_seconds": 20.0,
            "success_rate": 70.0,
        }
        mock_result = MagicMock()
        mock_result.to_dict.return_value = result_with_failures
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo", "--verbose"])

        assert result.exit_code == 0
        assert "Failed PRs" in result.stdout
        assert "PR #100" in result.stdout
        assert "API error" in result.stdout


class TestSyncRepoOutput:
    """Tests for sync repo command output formatting."""

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_text_output_shows_summary(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """Text output shows summary statistics."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo"])

        assert result.exit_code == 0
        assert "Sync Complete" in result.stdout
        assert "Created:" in result.stdout
        assert "Updated:" in result.stdout
        assert "Total discovered:" in result.stdout
        assert "Duration:" in result.stdout
        assert "Success rate:" in result.stdout


class TestSyncRepoShortFlags:
    """Tests for sync repo short flag aliases."""

    def test_help_shows_short_flags(self):
        """Help text shows short flag aliases."""
        result = runner.invoke(app, ["sync", "repo", "--help"])
        assert "-v" in result.stdout  # --verbose
        assert "-q" in result.stdout  # --quiet
        assert "-f" in result.stdout  # --format
        assert "-s" in result.stdout  # --state
        assert "-m" in result.stdout  # --max

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.BulkPRIngestionService")
    @patch("github_activity_db.cli.sync.RateLimitMonitor")
    @patch("github_activity_db.cli.sync.RequestPacer")
    @patch("github_activity_db.cli.sync.RequestScheduler")
    def test_short_format_flag_works(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """-f json works same as --format json."""
        # Setup mocks
        mock_result = MagicMock()
        mock_result.to_dict.return_value = mock_bulk_ingestion_result
        mock_service_instance = MagicMock()
        mock_service_instance.ingest_repository = AsyncMock(return_value=mock_result)
        mock_service_class.return_value = mock_service_instance

        mock_session = MagicMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_client_instance = MagicMock()
        mock_client_instance._github = MagicMock()
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        result = runner.invoke(app, ["sync", "repo", "owner/repo", "-f", "json"])

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["total_discovered"] == 10
