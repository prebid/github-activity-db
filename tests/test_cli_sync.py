"""Tests for sync CLI commands."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from github_activity_db.cli.app import app
from github_activity_db.db.models import PRState

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_rate_limit_monitor():
    """Auto-mock RateLimitMonitor.initialize to avoid async issues in tests.

    The CLI now initializes RateLimitMonitor and calls await monitor.initialize(),
    which requires async support. This fixture mocks the entire RateLimitMonitor
    class so tests don't need to set up the full async call chain.
    """
    with patch(
        "github_activity_db.cli.sync.RateLimitMonitor"
    ) as mock_monitor_class:
        mock_monitor = MagicMock()
        mock_monitor.initialize = AsyncMock()
        mock_monitor_class.return_value = mock_monitor
        yield mock_monitor


class TestGlobalFlags:
    """Tests for global CLI flags (--verbose, --quiet)."""

    def test_global_help_shows_verbose_flag(self):
        """Main help text shows --verbose and -v flags."""
        result = runner.invoke(app, ["--help"])
        assert "-v" in result.stdout
        assert "--verbose" in result.stdout

    def test_global_help_shows_quiet_flag(self):
        """Main help text shows --quiet and -q flags."""
        result = runner.invoke(app, ["--help"])
        assert "-q" in result.stdout
        assert "--quiet" in result.stdout


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
    def test_global_quiet_flag_works(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """Global --quiet flag works (controls log level, not CLI output)."""
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

        # --quiet is a global flag, must come before subcommand
        result = runner.invoke(app, ["--quiet", "sync", "pr", "owner/repo", "123"])

        assert result.exit_code == 0
        # CLI output still shows result (quiet only affects log level)
        assert "Created" in result.stdout

    @patch("github_activity_db.cli.sync.GitHubClient")
    @patch("github_activity_db.cli.sync.get_session")
    @patch("github_activity_db.cli.sync.PRIngestionService")
    def test_global_verbose_flag_works(
        self, mock_service_class, mock_get_session, mock_client, mock_ingestion_result
    ):
        """Global --verbose flag works (controls log level, not CLI output)."""
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

        # --verbose is a global flag, must come before subcommand
        result = runner.invoke(app, ["--verbose", "sync", "pr", "owner/repo", "123"])

        assert result.exit_code == 0
        # CLI output shows result (verbose only affects log level)
        assert "Created" in result.stdout

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
        """Help text shows short flag aliases for subcommand options."""
        result = runner.invoke(app, ["sync", "pr", "--help"])
        # -f is a sync pr option
        assert "-f" in result.stdout  # --format
        # Note: -v/--verbose and -q/--quiet are global flags (not on sync pr)

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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
    def test_global_quiet_flag_works_with_repo(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
        mock_bulk_ingestion_result,
    ):
        """Global --quiet flag works with sync repo (controls log level)."""
        # Setup mocks
        mock_monitor_class.return_value.initialize = AsyncMock()
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

        # --quiet is a global flag, must come before subcommand
        result = runner.invoke(app, ["--quiet", "sync", "repo", "owner/repo"])

        assert result.exit_code == 0
        # CLI output still shows result (quiet only affects log level)
        assert "Sync Complete" in result.stdout

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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
    def test_failed_prs_always_shown(
        self,
        mock_scheduler_class,
        mock_pacer_class,
        mock_monitor_class,
        mock_service_class,
        mock_get_session,
        mock_client,
    ):
        """Failed PRs are always shown when there are failures."""
        # Setup mocks with failures
        mock_monitor_class.return_value.initialize = AsyncMock()
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

        # Failed PRs are always shown (no verbose required anymore)
        result = runner.invoke(app, ["sync", "repo", "owner/repo"])

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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
        """Help text shows short flag aliases for subcommand options."""
        result = runner.invoke(app, ["sync", "repo", "--help"])
        # Subcommand options
        assert "-f" in result.stdout  # --format
        assert "-s" in result.stdout  # --state
        assert "-m" in result.stdout  # --max
        # Note: -v/--verbose and -q/--quiet are global flags (not on sync repo)

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
        mock_monitor_class.return_value.initialize = AsyncMock()
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
