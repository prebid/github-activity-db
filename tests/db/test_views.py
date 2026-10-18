"""The consumer views on seeded data (redesign plan 5.1, D4, D5, D28)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from github_activity_db.db.models import Base
from tests.db.alembic_support import query
from tests.db.schema_support import views
from tests.db.seed import T0, Seed, connect

REPO = "prebid/Prebid.js"

COLUMNS = {
    "v_github_users": ["github_id", "node_id", "login", "type", "deleted_at", "is_automation"],
    "v_pr_reviewers": [
        "repository_full_name",
        "pr_number",
        "pr_github_id",
        "reviewer_github_id",
        "reviewer_login",
        "final_verdict",
        "comment_count",
    ],
    "v_pr_commenters": [
        "repository_full_name",
        "pr_number",
        "pr_github_id",
        "commenter_github_id",
        "commenter_login",
        "comment_count",
    ],
    "v_commit_authors": [
        "repository_full_name",
        "pr_number",
        "pr_github_id",
        "sha",
        "author_github_id",
        "author_name",
        "author_email",
        "authored_at",
        "is_automation",
    ],
}


@pytest.fixture
def seed(migrated_database: Path) -> Iterator[Seed]:
    with connect(migrated_database) as connection:
        yield Seed(connection)


def rows(seed: Seed, sql: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in seed.connection.exec_driver_sql(sql).fetchall()]


def github_id(seed: Seed, user_id: int) -> int:
    ((value,),) = rows(seed, f"SELECT github_id FROM github_users WHERE id = {user_id}")
    return int(str(value))


class TestShape:
    def test_every_view_lists_its_columns(self, head_template: Path) -> None:
        assert set(views(head_template)) == set(COLUMNS)
        for view, expected in COLUMNS.items():
            assert [
                row[1] for row in query(head_template, f"PRAGMA table_info({view})")
            ] == expected

    def test_views_identify_rows_by_consumer_keys_not_row_ids(self) -> None:
        """Row ids are each table's ``id`` and the foreign keys to it; they change on a rebuild."""
        row_ids = {"id"} | {
            foreign_key.parent.name
            for table in Base.metadata.tables.values()
            for foreign_key in table.foreign_keys
        }
        assert {"id", "pr_id", "author_id", "reviewer_id", "repository_id"} <= row_ids
        for view, columns in COLUMNS.items():
            assert row_ids.isdisjoint(columns), view
        assert "refreshed_at" not in COLUMNS["v_github_users"]


class TestReviewers:
    @pytest.mark.parametrize(
        ("reviews", "verdict"),
        [
            pytest.param([(1, "APPROVED"), (2, "DISMISSED")], "DISMISSED", id="later DISMISSED"),
            pytest.param(
                [(1, "CHANGES_REQUESTED"), (2, "COMMENTED")],
                "CHANGES_REQUESTED",
                id="later COMMENTED",
            ),
            pytest.param([(1, "COMMENTED"), (2, "COMMENTED")], "COMMENTED", id="only COMMENTED"),
            pytest.param([(1, "DISMISSED"), (2, "APPROVED")], "APPROVED", id="approved again"),
            pytest.param(
                # Inserted in this order: the later row has the lower review id.
                [(9, "CHANGES_REQUESTED"), (4, "APPROVED")],
                "CHANGES_REQUESTED",
                id="order by review id",
            ),
            pytest.param(
                [(1, "APPROVED"), (2, "CHANGES_REQUESTED"), (3, "COMMENTED")],
                "CHANGES_REQUESTED",
                id="latest verdict",
            ),
        ],
    )
    def test_final_verdict(self, seed: Seed, reviews: list[tuple[int, str]], verdict: str) -> None:
        pr = seed.pull_request(seed.repository(REPO), 13481, seed.user("author"))
        reviewer = seed.user("reviewer")
        base = seed.next_id() * 100
        for offset, state in reviews:
            seed.review(pr, base + offset, reviewer, state)
        assert rows(seed, "SELECT reviewer_login, final_verdict FROM v_pr_reviewers") == [
            ("reviewer", verdict)
        ]

    def test_each_pull_request_has_its_own_verdict(self, seed: Seed) -> None:
        repository = seed.repository(REPO)
        reviewer = seed.user("reviewer")
        first = seed.pull_request(repository, 1, None)
        second = seed.pull_request(repository, 2, None)
        seed.review(first, seed.next_id(), reviewer, "APPROVED")
        seed.review(second, seed.next_id(), reviewer, "CHANGES_REQUESTED")
        # The later review id is on the second PR; a verdict not scoped to its PR would leak.
        assert rows(
            seed, "SELECT pr_number, final_verdict FROM v_pr_reviewers ORDER BY pr_number"
        ) == [(1, "APPROVED"), (2, "CHANGES_REQUESTED")]

    def test_excludes_the_author_automation_and_null_reviewers(self, seed: Seed) -> None:
        author = seed.user("author")
        bot = seed.user("dependabot", type="Bot")
        listed = seed.user("prebidjs-release")
        seed.automation_account(github_id(seed, listed), "prebidjs-release")
        reviewer = seed.user("reviewer")
        pr = seed.pull_request(seed.repository(REPO), 13481, author, github_id=4891210785)
        for account in (author, bot, listed, None, reviewer):
            seed.review(pr, seed.next_id(), account, "APPROVED")
        assert rows(seed, "SELECT * FROM v_pr_reviewers") == [
            (REPO, 13481, 4891210785, github_id(seed, reviewer), "reviewer", "APPROVED", 0)
        ]

    def test_a_pull_request_without_an_author_keeps_every_reviewer(self, seed: Seed) -> None:
        pr = seed.pull_request(seed.repository(REPO), 1, None)
        for login in ("first", "second"):
            seed.review(pr, seed.next_id(), seed.user(login), "COMMENTED")
        assert sorted(rows(seed, "SELECT reviewer_login FROM v_pr_reviewers")) == [
            ("first",),
            ("second",),
        ]

    def test_comment_count_is_the_reviewers_comments_on_that_pull_request(self, seed: Seed) -> None:
        repository = seed.repository(REPO)
        reviewer = seed.user("reviewer")
        reviewed = seed.pull_request(repository, 1, seed.user("author"))
        other = seed.pull_request(repository, 2, None)
        seed.review(reviewed, seed.next_id(), reviewer, "APPROVED")
        seed.comment(reviewed, reviewer)
        seed.comment(reviewed, reviewer)
        seed.comment(other, reviewer)
        assert rows(seed, "SELECT pr_number, comment_count FROM v_pr_reviewers") == [(1, 2)]
        # No review on the other PR: there they are a commenter.
        assert rows(
            seed, "SELECT pr_number, commenter_login, comment_count FROM v_pr_commenters"
        ) == [(2, "reviewer", 1)]

    def test_a_deleted_account_keeps_its_reviews(self, seed: Seed) -> None:
        pr = seed.pull_request(seed.repository(REPO), 1, None)
        seed.review(pr, seed.next_id(), seed.user("gone", deleted_at=T0), "APPROVED")
        assert rows(seed, "SELECT reviewer_login FROM v_pr_reviewers") == [("gone",)]


class TestCommenters:
    def test_excludes_reviewers_the_author_automation_and_null_authors(self, seed: Seed) -> None:
        author = seed.user("author")
        bot = seed.user("github-actions", type="Bot")
        listed = seed.user("prebidjs-release")
        seed.automation_account(github_id(seed, listed), "prebidjs-release")
        reviewer = seed.user("reviewer")
        commenter = seed.user("commenter")
        pr = seed.pull_request(seed.repository(REPO), 7, author, github_id=77)
        seed.review(pr, seed.next_id(), reviewer, "COMMENTED")
        for account in (author, bot, listed, None, reviewer, commenter, commenter, commenter):
            seed.comment(pr, account)
        assert rows(seed, "SELECT * FROM v_pr_commenters") == [
            (REPO, 7, 77, github_id(seed, commenter), "commenter", 3)
        ]

    def test_counts_are_per_pull_request(self, seed: Seed) -> None:
        repository = seed.repository(REPO)
        commenter = seed.user("commenter")
        first = seed.pull_request(repository, 1, None)
        second = seed.pull_request(repository, 2, None)
        for pr in (first, first, second):
            seed.comment(pr, commenter)
        assert rows(
            seed, "SELECT pr_number, comment_count FROM v_pr_commenters ORDER BY pr_number"
        ) == [(1, 2), (2, 1)]

    def test_a_pull_request_without_an_author_keeps_every_commenter(self, seed: Seed) -> None:
        pr = seed.pull_request(seed.repository(REPO), 1, None)
        seed.comment(pr, seed.user("commenter"))
        assert rows(seed, "SELECT commenter_login, comment_count FROM v_pr_commenters") == [
            ("commenter", 1)
        ]


class TestGitHubUsers:
    def test_identifies_accounts_by_github_ids_not_row_ids(self, seed: Seed) -> None:
        seed.user("first", github_id=583231, node_id="MDQ6VXNlcjU4MzIzMQ==")
        seed.user("second", github_id=49699333, node_id="MDM6Qm90NDk2OTkzMzM=")
        assert rows(seed, "SELECT id FROM github_users ORDER BY id") == [(1,), (2,)]
        assert rows(
            seed, "SELECT github_id, node_id, login FROM v_github_users ORDER BY login"
        ) == [
            (583231, "MDQ6VXNlcjU4MzIzMQ==", "first"),
            (49699333, "MDM6Qm90NDk2OTkzMzM=", "second"),
        ]

    def test_flags_bots_and_listed_accounts(self, seed: Seed) -> None:
        seed.user("person")
        seed.user("dependabot", type="Bot")
        listed = seed.user("prebidjs-release")
        seed.automation_account(github_id(seed, listed), "prebidjs-release")
        seed.user("gone", type="Mannequin", deleted_at=T0)
        assert sorted(
            rows(
                seed,
                "SELECT login, type, deleted_at IS NOT NULL, is_automation FROM v_github_users",
            )
        ) == [
            ("dependabot", "Bot", 0, 1),
            ("gone", "Mannequin", 1, 0),
            ("person", "User", 0, 0),
            ("prebidjs-release", "User", 0, 1),
        ]


class TestCommitAuthors:
    def test_automation_by_account_type_listing_and_email(self, seed: Seed) -> None:
        person = seed.user("person")
        bot = seed.user("dependabot[bot]", type="Bot")
        listed = seed.user("claude")
        seed.automation_account(github_id(seed, listed), "claude")
        seed.automation_email("noreply@anthropic.com")
        pr = seed.pull_request(seed.repository(REPO), 3, person, github_id=33)
        seed.commit(pr, "1" * 40, person)
        seed.commit(pr, "2" * 40, bot)
        seed.commit(pr, "3" * 40, listed)
        seed.commit(pr, "4" * 40, None, author_email="NoReply@Anthropic.com")
        seed.commit(pr, "5" * 40, None, author_email=None)
        assert rows(
            seed,
            "SELECT repository_full_name, pr_number, pr_github_id, sha, author_github_id, "
            "is_automation FROM v_commit_authors ORDER BY sha",
        ) == [
            (REPO, 3, 33, "1" * 40, github_id(seed, person), 0),
            (REPO, 3, 33, "2" * 40, github_id(seed, bot), 1),
            (REPO, 3, 33, "3" * 40, github_id(seed, listed), 1),
            (REPO, 3, 33, "4" * 40, None, 1),
            (REPO, 3, 33, "5" * 40, None, 0),
        ]

    def test_an_automation_email_flags_a_commit_linked_to_a_person(self, seed: Seed) -> None:
        """The email arm applies to linked commits too: a person's account committing as
        noreply@anthropic.com is automation for that commit only."""
        person = seed.user("person")
        seed.automation_email("noreply@anthropic.com")
        pr = seed.pull_request(seed.repository(REPO), 3, person)
        seed.commit(pr, "1" * 40, person, author_email="noreply@anthropic.com")
        seed.commit(pr, "2" * 40, person, author_email="person@example.com")
        assert rows(
            seed, "SELECT sha, author_github_id, is_automation FROM v_commit_authors ORDER BY sha"
        ) == [
            ("1" * 40, github_id(seed, person), 1),
            ("2" * 40, github_id(seed, person), 0),
        ]
        assert rows(seed, "SELECT is_automation FROM v_github_users") == [(0,)]

    def test_carries_the_git_author_fields(self, seed: Seed) -> None:
        pr = seed.pull_request(seed.repository(REPO), 3, None)
        seed.commit(pr, "a" * 40, None, author_name="Dev", author_email="dev@example.com")
        assert rows(
            seed, "SELECT author_name, author_email, authored_at FROM v_commit_authors"
        ) == [("Dev", "dev@example.com", "2026-01-01 00:00:00.000000")]


class TestScoping:
    def test_each_row_comes_from_its_own_repository_pull_request_and_account(
        self, seed: Seed
    ) -> None:
        """Two repositories, and on one PR two reviewers and two commenters whose verdicts and
        counts differ; the highest review id belongs to no account."""
        first, second = seed.repository("prebid/a"), seed.repository("prebid/b")
        r1, r2, c1, c2 = (seed.user(login) for login in ("r1", "r2", "c1", "c2"))
        pr = seed.pull_request(first, 1, None)
        other = seed.pull_request(second, 2, None)
        base = seed.next_id() * 100
        seed.review(pr, base + 10, r1, "CHANGES_REQUESTED")
        seed.review(pr, base + 11, r2, "CHANGES_REQUESTED")
        seed.review(pr, base + 12, r1, "APPROVED")
        seed.review(pr, base + 13, None, "DISMISSED")
        seed.review(other, base + 14, r2, "APPROVED")
        for account in (r1, r1, r2, c1, c1, c2):
            seed.comment(pr, account)
        seed.comment(other, c2)
        seed.commit(pr, "1" * 40)
        seed.commit(other, "2" * 40)
        assert rows(
            seed,
            "SELECT repository_full_name, pr_number, reviewer_login, final_verdict, comment_count "
            "FROM v_pr_reviewers ORDER BY pr_number, reviewer_login",
        ) == [
            ("prebid/a", 1, "r1", "APPROVED", 2),
            ("prebid/a", 1, "r2", "CHANGES_REQUESTED", 1),
            ("prebid/b", 2, "r2", "APPROVED", 0),
        ]
        assert rows(
            seed,
            "SELECT repository_full_name, pr_number, commenter_login, comment_count "
            "FROM v_pr_commenters ORDER BY pr_number, commenter_login",
        ) == [("prebid/a", 1, "c1", 2), ("prebid/a", 1, "c2", 1), ("prebid/b", 2, "c2", 1)]
        assert rows(
            seed, "SELECT repository_full_name, pr_number, sha FROM v_commit_authors ORDER BY sha"
        ) == [("prebid/a", 1, "1" * 40), ("prebid/b", 2, "2" * 40)]
