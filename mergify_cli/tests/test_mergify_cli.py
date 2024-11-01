#
#  Copyright © 2021-2024 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import collections
import json
import pathlib
import subprocess
import typing
from unittest import mock

import pytest
import respx

from mergify_cli import utils
from mergify_cli.stack import push
from mergify_cli.tests import utils as test_utils


@pytest.fixture(autouse=True)
def _unset_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "whatever")


@pytest.fixture(autouse=True)
def _change_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    # Change working directory to avoid doing git commands in the current
    # repository
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def _git_repo() -> None:
    subprocess.call(["git", "init", "--initial-branch=main"])
    subprocess.call(["git", "config", "user.email", "test@example.com"])
    subprocess.call(["git", "config", "user.name", "Test User"])
    subprocess.call(["git", "commit", "--allow-empty", "-m", "Initial commit"])
    subprocess.call(["git", "config", "--add", "branch.main.merge", "refs/heads/main"])
    subprocess.call(["git", "config", "--add", "branch.main.remote", "origin"])


@pytest.fixture
def git_mock(
    tmp_path: pathlib.Path,
) -> typing.Generator[test_utils.GitMock, None, None]:
    git_mock_object = test_utils.GitMock()
    # Top level directory is a temporary path
    git_mock_object.mock("rev-parse", "--show-toplevel", output=str(tmp_path))
    # Name of the current branch
    git_mock_object.mock("rev-parse", "--abbrev-ref", "HEAD", output="current-branch")
    # URL of the GitHub repository
    git_mock_object.mock(
        "config",
        "--get",
        "remote.origin.url",
        output="https://github.com/user/repo",
    )
    # Mock pull and push commands
    git_mock_object.mock("pull", "--rebase", "origin", "main", output="")
    git_mock_object.mock(
        "push",
        "-f",
        "origin",
        "current-branch:current-branch/aio",
        output="",
    )

    with mock.patch("mergify_cli.utils.git", git_mock_object):
        yield git_mock_object


@pytest.mark.usefixtures("_git_repo")
async def test_get_branch_name() -> None:
    assert await utils.git_get_branch_name() == "main"


@pytest.mark.usefixtures("_git_repo")
async def test_get_target_branch() -> None:
    assert await utils.git_get_target_branch("main") == "main"


@pytest.mark.usefixtures("_git_repo")
async def test_get_target_remote() -> None:
    assert await utils.git_get_target_remote("main") == "origin"


@pytest.mark.usefixtures("_git_repo")
async def test_get_trunk() -> None:
    assert await utils.get_trunk() == "origin/main"


@pytest.mark.parametrize(
    "valid_branch_name",
    [
        ("my-branch"),
        ("prefix/my-branch"),
        ("my-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50"),
    ],
)
def test_check_local_branch_valid(valid_branch_name: str) -> None:
    # Should not raise an error
    push.check_local_branch(
        branch_name=valid_branch_name,
        branch_prefix="prefix",
    )


def test_check_local_branch_invalid() -> None:
    with pytest.raises(
        push.LocalBranchInvalidError,
        match="Local branch is a branch generated by Mergify CLI",
    ):
        push.check_local_branch(
            branch_name="prefix/my-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
            branch_prefix="prefix",
        )


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_create(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    # Mock 2 commits on branch `current-branch`
    git_mock.commit(
        test_utils.Commit(
            sha="commit1_sha",
            title="Title commit 1",
            message="Message commit 1",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf50",
        ),
    )
    git_mock.commit(
        test_utils.Commit(
            sha="commit2_sha",
            title="Title commit 2",
            message="Message commit 2",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf51",
        ),
    )

    # Mock HTTP calls
    respx_mock.get("/user").respond(200, json={"login": "author"})
    respx_mock.get("/repos/user/repo").respond(
        200,
        json={"id": 123456},
    )
    respx_mock.get("/search/issues").respond(200, json={"items": []})
    post_pull1_mock = respx_mock.post(
        "/repos/user/repo/pulls",
        json__title="Title commit 1",
    ).respond(
        200,
        json={
            "html_url": "https://github.com/repo/user/pull/1",
            "number": "1",
            "title": "Title commit 1",
            "head": {"sha": "commit1_sha"},
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
        },
    )
    post_pull2_mock = respx_mock.post(
        "/repos/user/repo/pulls",
        json__title="Title commit 2",
    ).respond(
        200,
        json={
            "html_url": "https://github.com/repo/user/pull/2",
            "number": "2",
            "title": "Title commit 2",
            "head": {"sha": "commit2_sha"},
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
        },
    )
    respx_mock.get("/repos/user/repo/issues/1/comments").respond(200, json=[])
    post_comment1_mock = respx_mock.post("/repos/user/repo/issues/1/comments").respond(
        200,
    )
    respx_mock.get("/repos/user/repo/issues/2/comments").respond(200, json=[])
    post_comment2_mock = respx_mock.post("/repos/user/repo/issues/2/comments").respond(
        200,
    )

    await push.stack_push(
        github_server="https://api.github.com/",
        token="",
        skip_rebase=False,
        next_only=False,
        branch_prefix="",
        dry_run=False,
        trunk=("origin", "main"),
    )

    # First pull request is created
    assert len(post_pull1_mock.calls) == 1
    assert json.loads(post_pull1_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "base": "main",
        "title": "Title commit 1",
        "body": "Message commit 1",
        "draft": False,
    }

    # Second pull request is created
    assert len(post_pull2_mock.calls) == 1
    assert json.loads(post_pull2_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf51",
        "base": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "title": "Title commit 2",
        "body": "Message commit 2\n\nDepends-On: #1",
        "draft": False,
    }

    # First stack comment is created
    assert len(post_comment1_mock.calls) == 1
    expected_body = """This pull request is part of a stack:
1. Title commit 1 ([#1](https://github.com/repo/user/pull/1)) 👈
1. Title commit 2 ([#2](https://github.com/repo/user/pull/2))
"""
    assert json.loads(post_comment1_mock.calls.last.request.content) == {
        "body": expected_body,
    }

    # Second stack comment is created
    assert len(post_comment2_mock.calls) == 1
    expected_body = """This pull request is part of a stack:
1. Title commit 1 ([#1](https://github.com/repo/user/pull/1))
1. Title commit 2 ([#2](https://github.com/repo/user/pull/2)) 👈
"""
    assert json.loads(post_comment2_mock.calls.last.request.content) == {
        "body": expected_body,
    }


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_create_single_pull(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    # Mock 1 commits on branch `current-branch`
    git_mock.commit(
        test_utils.Commit(
            sha="commit1_sha",
            title="Title commit 1",
            message="Message commit 1",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf50",
        ),
    )

    # Mock HTTP calls
    respx_mock.get("/user").respond(200, json={"login": "author"})
    respx_mock.get("/repos/user/repo").respond(
        200,
        json={"id": 123456},
    )
    respx_mock.get("/search/issues").respond(200, json={"items": []})

    post_pull_mock = respx_mock.post(
        "/repos/user/repo/pulls",
        json__title="Title commit 1",
    ).respond(
        200,
        json={
            "html_url": "https://github.com/repo/user/pull/1",
            "number": "1",
            "title": "Title commit 1",
            "head": {"sha": "commit1_sha"},
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
        },
    )
    respx_mock.get("/repos/user/repo/issues/1/comments").respond(200, json=[])

    await push.stack_push(
        github_server="https://api.github.com/",
        token="",
        skip_rebase=False,
        next_only=False,
        branch_prefix="",
        dry_run=False,
        trunk=("origin", "main"),
    )

    # Pull request is created without stack comment
    assert len(post_pull_mock.calls) == 1
    assert json.loads(post_pull_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "base": "main",
        "title": "Title commit 1",
        "body": "Message commit 1",
        "draft": False,
    }


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_update_no_rebase(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    # Mock 1 commits on branch `current-branch`
    git_mock.commit(
        test_utils.Commit(
            sha="commit_sha",
            title="Title",
            message="Message",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf50",
        ),
    )

    # Mock HTTP calls: the stack already exists but it's out of date, it should
    # be updated
    respx_mock.get("/user").respond(200, json={"login": "author"})
    respx_mock.get("/repos/user/repo").respond(
        200,
        json={"id": 123456},
    )
    respx_mock.get("/search/issues").respond(
        200,
        json={
            "items": [
                {
                    "pull_request": {
                        "url": "https://api.github.com/repos/user/repo/pulls/123",
                    },
                },
            ],
        },
    )

    respx_mock.get(
        "/repos/user/repo/pulls/123",
    ).respond(
        200,
        json={
            "html_url": "",
            "number": "123",
            "title": "Title",
            "head": {
                "sha": "previous_commit_sha",
                "ref": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
            },
            "body": "body",
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
        },
    )
    patch_pull_mock = respx_mock.patch("/repos/user/repo/pulls/123").respond(
        200,
        json={},
    )
    respx_mock.get("/repos/user/repo/issues/123/comments").respond(
        200,
        json=[
            {
                "body": "This pull request is part of a stack:\n...",
                "url": "https://api.github.com/repos/user/repo/issues/comments/456",
            },
        ],
    )
    respx_mock.patch("/repos/user/repo/issues/comments/456").respond(200)

    await push.stack_push(
        github_server="https://api.github.com/",
        token="",
        skip_rebase=True,
        next_only=False,
        branch_prefix="",
        dry_run=False,
        trunk=("origin", "main"),
    )
    assert not git_mock.has_been_called_with("pull", "--rebase", "origin", "main")

    # The pull request is updated
    assert len(patch_pull_mock.calls) == 1
    assert json.loads(patch_pull_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "base": "main",
        "title": "Title",
        "body": "Message",
    }


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_update(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    # Mock 1 commits on branch `current-branch`
    git_mock.commit(
        test_utils.Commit(
            sha="commit_sha",
            title="Title",
            message="Message",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf50",
        ),
    )

    # Mock HTTP calls: the stack already exists but it's out of date, it should
    # be updated
    respx_mock.get("/user").respond(200, json={"login": "author"})
    respx_mock.get("/repos/user/repo").respond(
        200,
        json={"id": 123456},
    )
    respx_mock.get("/search/issues").respond(
        200,
        json={
            "items": [
                {
                    "pull_request": {
                        "url": "https://api.github.com/repos/user/repo/pulls/123",
                    },
                },
            ],
        },
    )

    respx_mock.get(
        "/repos/user/repo/pulls/123",
    ).respond(
        200,
        json={
            "html_url": "",
            "number": "123",
            "title": "Title",
            "head": {
                "sha": "previous_commit_sha",
                "ref": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
            },
            "body": "body",
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
        },
    )
    patch_pull_mock = respx_mock.patch("/repos/user/repo/pulls/123").respond(
        200,
        json={},
    )
    respx_mock.get("/repos/user/repo/issues/123/comments").respond(
        200,
        json=[
            {
                "body": "This pull request is part of a stack:\n...",
                "url": "https://api.github.com/repos/user/repo/issues/comments/456",
            },
        ],
    )
    respx_mock.patch("/repos/user/repo/issues/comments/456").respond(200)

    await push.stack_push(
        github_server="https://api.github.com/",
        token="",
        skip_rebase=False,
        next_only=False,
        branch_prefix="",
        dry_run=False,
        trunk=("origin", "main"),
    )
    assert git_mock.has_been_called_with("pull", "--rebase", "origin", "main")

    # The pull request is updated
    assert len(patch_pull_mock.calls) == 1
    assert json.loads(patch_pull_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "base": "main",
        "title": "Title",
        "body": "Message",
    }


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_update_keep_title_and_body(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    # Mock 1 commits on branch `current-branch`
    git_mock.commit(
        test_utils.Commit(
            sha="commit_sha",
            title="New Title that should be ignored",
            message="New Message that should be ignored",
            change_id="I29617d37762fd69809c255d7e7073cb11f8fbf50",
        ),
    )

    # Mock HTTP calls: the stack already exists but it's out of date, it should
    # be updated
    respx_mock.get("/user").respond(200, json={"login": "author"})
    respx_mock.get("/repos/user/repo").respond(
        200,
        json={"id": 123456},
    )
    respx_mock.get("/search/issues").respond(
        200,
        json={
            "items": [
                {
                    "pull_request": {
                        "url": "https://api.github.com/repos/user/repo/pulls/123",
                    },
                },
            ],
        },
    )
    respx_mock.get(
        "/repos/user/repo/pulls/123",
    ).respond(
        200,
        json={
            "html_url": "",
            "number": "123",
            "title": "Title",
            "head": {
                "sha": "previous_commit_sha",
                "ref": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
            },
            "state": "open",
            "merged_at": None,
            "draft": False,
            "node_id": "",
            "body": "DONT TOUCH ME\n\nDepends-On: #12345\n",
        },
    )
    patch_pull_mock = respx_mock.patch("/repos/user/repo/pulls/123").respond(
        200,
        json={},
    )
    respx_mock.get("/repos/user/repo/issues/123/comments").respond(
        200,
        json=[
            {
                "body": "This pull request is part of a stack:\n...",
                "url": "https://api.github.com/repos/user/repo/issues/comments/456",
            },
        ],
    )
    respx_mock.patch("/repos/user/repo/issues/comments/456").respond(200)

    await push.stack_push(
        github_server="https://api.github.com/",
        token="",
        skip_rebase=False,
        next_only=False,
        branch_prefix="",
        dry_run=False,
        trunk=("origin", "main"),
        keep_pull_request_title_and_body=True,
    )

    # The pull request is updated
    assert len(patch_pull_mock.calls) == 1
    assert json.loads(patch_pull_mock.calls.last.request.content) == {
        "head": "current-branch/I29617d37762fd69809c255d7e7073cb11f8fbf50",
        "base": "main",
        "body": "DONT TOUCH ME",
    }


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_on_destination_branch_raises_an_error(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("/user").respond(200, json={"login": "author"})
    git_mock.mock("rev-parse", "--abbrev-ref", "HEAD", output="main")

    with pytest.raises(SystemExit, match="1"):
        await push.stack_push(
            github_server="https://api.github.com/",
            token="",
            skip_rebase=False,
            next_only=False,
            branch_prefix="",
            dry_run=False,
            trunk=("origin", "main"),
        )


@pytest.mark.respx(base_url="https://api.github.com/")
async def test_stack_without_common_commit_raises_an_error(
    git_mock: test_utils.GitMock,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("/user").respond(200, json={"login": "author"})
    git_mock.mock("merge-base", "--fork-point", "origin/main", output="")

    with pytest.raises(SystemExit, match="1"):
        await push.stack_push(
            github_server="https://api.github.com/",
            token="",
            skip_rebase=False,
            next_only=False,
            branch_prefix="",
            dry_run=False,
            trunk=("origin", "main"),
        )


@pytest.mark.parametrize(
    ("default_arg_fct", "config_get_result", "expected_default"),
    [
        (utils.get_default_keep_pr_title_body, "true", True),
        (
            lambda: utils.get_default_branch_prefix("author"),
            "dummy-prefix",
            "dummy-prefix",
        ),
    ],
)
async def test_defaults_config_args_set(
    default_arg_fct: collections.abc.Callable[
        [],
        collections.abc.Awaitable[bool | str],
    ],
    config_get_result: bytes,
    expected_default: bool,
) -> None:
    with mock.patch.object(utils, "run_command", return_value=config_get_result):
        assert (await default_arg_fct()) == expected_default
