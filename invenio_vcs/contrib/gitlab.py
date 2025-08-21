from __future__ import annotations

from typing import Any

import dateutil
import gitlab
import gitlab.const
import requests
from flask import current_app
from invenio_oauthclient import current_oauthclient
from werkzeug.utils import cached_property

from invenio_vcs.providers import (
    GenericContributor,
    GenericOwner,
    GenericOwnerType,
    GenericRelease,
    GenericRepository,
    GenericUser,
    GenericWebhook,
    RepositoryServiceProvider,
    RepositoryServiceProviderFactory,
)


def _gl_response_error_handler(f):
    def inner_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except gitlab.GitlabGetError as e:
            if e.response_code == 404:
                return None
            else:
                raise e
        except gitlab.GitlabCreateError as e:
            if e.response_code == 404:
                return None
            else:
                raise e

    return inner_function


class GitLabProviderFactory(RepositoryServiceProviderFactory):
    def __init__(
        self,
        base_url: str,
        webhook_receiver_url: str,
        id="gitlab",
        name="GitLab",
        description="Automatically archive your repositories",
        credentials_key="GITLAB_APP_CREDENTIALS",
        config={},
    ):
        super().__init__(
            GitLabProvider,
            base_url=base_url,
            webhook_receiver_url=webhook_receiver_url,
            id=id,
            name=name,
            description=description,
            credentials_key=credentials_key,
            icon="gitlab",
            repository_name="project",
            repository_name_plural="projects",
        )
        self._config = dict()
        self._config.update(shared_validation_token="")
        self._config.update(config)

    def _account_info_handler(self, remote, resp: dict):
        gl = gitlab.Gitlab(
            self.base_url,
            oauth_token=resp["access_token"],
        )
        gl.auth()
        user_attrs = gl.user.attributes
        handlers = current_oauthclient.signup_handlers[remote.name]
        return handlers["info_serializer"](resp, user_attrs)

    def _account_info_serializer(self, remote, resp, user_info, **kwargs):
        return dict(
            user=dict(
                email=user_info["email"],
                profile=dict(
                    username=user_info["username"],
                    full_name=user_info["name"],
                ),
            ),
            external_id=str(user_info["id"]),
            external_method="gitlab",
        )

    @property
    def remote_config(self):
        return dict(
            title=self.name,
            description=self.description,
            icon="fa fa-{}".format(self.icon),
            authorized_handler="invenio_oauthclient.handlers:authorized_signup_handler",
            disconnect_handler=self.oauth_handlers.disconnect_handler,
            signup_handler=dict(
                info=self._account_info_handler,
                info_serializer=self._account_info_serializer,
                setup=self.oauth_handlers.account_setup_handler,
                view="invenio_oauthclient.handlers:signup_handler",
            ),
            params=dict(
                base_url="{}/api/v4/".format(self.base_url),
                request_token_url=None,
                access_token_url="{}/oauth/token".format(self.base_url),
                access_token_method="POST",
                authorize_url="{}/oauth/authorize".format(self.base_url),
                app_key=self.credentials_key,
            ),
        )

    @property
    def config(self):
        return self._config

    def url_for_tag(self, repository_name, tag_name) -> str:
        return "{}/{}/-/tags/{}".format(self.base_url, repository_name, tag_name)

    def url_for_new_file(self, repository_name, branch_name, file_name) -> str:
        return "{}/{}/-/new/{}/?file_name={}".format(
            self.base_url, repository_name, branch_name, file_name
        )

    def url_for_new_release(self, repository_name) -> str:
        return "{}/{}/-/releases/new".format(self.base_url, repository_name)

    def webhook_is_create_release_event(self, event_payload: dict[str, Any]):
        # https://archives.docs.gitlab.com/17.11/user/project/integrations/webhook_events/#release-events

        # GitLab does not have unpublished/draft releases the way GitHub does. However, it does have
        # "upcoming releases" (https://archives.docs.gitlab.com/17.11/api/releases/#upcoming-releases)
        # meaning ones with a release date in the future.
        # TODO: do we want to return False for upcoming releases?

        object_kind = event_payload.get("object_kind")
        action = event_payload.get("action")

        # existing `invenio-gitlab` instead uses the `tag_push` event which is more general than the `release`
        # event (https://codebase.helmholtz.cloud/rodare/invenio-gitlab/-/blob/d66181697b8a34383b333306b559d13cd6fa829a/invenio_gitlab/receivers.py#L41).
        # TODO: I recommend using the `release` event as this is a more 'formal' manual action and better corresponds to the release event in GitHub. Is this okay?
        return object_kind == "release" and action == "create"

    def webhook_event_to_generic(
        self, event_payload: dict[str, Any]
    ) -> tuple[GenericRelease, GenericRepository]:
        # https://archives.docs.gitlab.com/17.11/user/project/integrations/webhook_events/#release-events

        zipball_url: str | None = None
        tarball_url: str | None = None

        for source in event_payload["sources"]:
            format = source["format"]
            url = source["url"]
            if format == "zip":
                zipball_url = url
            elif format == "tar":
                tarball_url = url

        release = GenericRelease(
            # GitLab does not expose the in-database ID of releases through the webhook payload or the REST API
            # It does exist internally but it's never sent to us
            id=event_payload["tag"],
            tag_name=event_payload["tag"],
            html_url=event_payload["url"],
            name=event_payload["name"],
            body=event_payload["description"],
            zipball_url=zipball_url,
            tarball_url=tarball_url,
            created_at=dateutil.parser.parse(event_payload["created_at"]),
            published_at=dateutil.parser.parse(event_payload["released_at"]),
        )

        repo = GitLabProviderFactory._proj_to_generic(event_payload["project"])
        return (release, repo)

    @staticmethod
    def _extract_license(proj_attrs: dict[str, Any]):
        license_obj = proj_attrs.get("license")
        if license_obj is not None:
            return license_obj["key"].upper()
        return None

    @staticmethod
    def _proj_to_generic(proj_attrs: dict[str, Any]):
        return GenericRepository(
            id=str(proj_attrs["id"]),
            full_name=proj_attrs["path_with_namespace"],
            default_branch=proj_attrs["default_branch"],
            html_url=proj_attrs["web_url"],
            description=proj_attrs["description"],
            license_spdx=GitLabProviderFactory._extract_license(proj_attrs),
        )


class GitLabProvider(RepositoryServiceProvider):
    @cached_property
    def _gl(self):
        gl = gitlab.Gitlab(self.factory.base_url, oauth_token=self.access_token)
        gl.auth()
        return gl

    @_gl_response_error_handler
    def list_repositories(self) -> dict[str, GenericRepository] | None:
        repos: dict[str, GenericRepository] = {}
        for project in self._gl.projects.list(
            iterator=True,
            simple=False,
            min_access_level=gitlab.const.MAINTAINER_ACCESS,
        ):
            repos[str(project.id)] = GenericRepository(
                id=str(project.id),
                full_name=project.path_with_namespace,
                default_branch=project.default_branch,
                html_url=project.web_url,
                description=project.description,
                # TODO: license is not returned in the projects list (only when querying an individual project).
                # This would be super slow. Do we really need license here?
                license_spdx=None,
            )
        return repos

    @_gl_response_error_handler
    def get_repository(self, repository_id: str) -> GenericRepository | None:
        assert repository_id.isdigit()
        proj = self._gl.projects.get(int(repository_id))
        return GitLabProviderFactory._proj_to_generic(proj.asdict())

    @_gl_response_error_handler
    def list_repository_contributors(
        self, repository_id: str, max: int
    ) -> list[GenericContributor] | None:
        assert repository_id.isdigit()
        proj = self._gl.projects.get(int(repository_id), lazy=True)

        contribs: list[GenericContributor] = []
        for index, contrib in enumerate(
            proj.repository_contributors(iterator=True, order_by="commits", sort="desc")
        ):
            email = contrib["email"]
            contrib_count = contrib["commits"]

            # repository_contributors returns a very small amount of data (not even the username)
            # See here https://archives.docs.gitlab.com/17.11/api/repositories/#contributors
            # So we try to enrich the data by searching for the user with the matching email.
            # We will fail to find it if a) the user doesn't exist (e.g. repos imported/forked from somewhere else)
            # or b) if the user has not made their email address public.
            # By default, email addresses on GitLab are private, so this is unlikely to succeed.
            matching_users = self._gl.users.list(search=email)
            if len(matching_users) == 0:
                contribs.append(
                    GenericContributor(
                        id=email,
                        username=email,
                        display_name=contrib["name"],
                        contributions_count=contrib_count,
                    )
                )
            else:
                matching_user = matching_users[0]
                contribs.append(
                    GenericContributor(
                        id=str(matching_user.id),
                        username=matching_user.username,
                        display_name=matching_user.name,
                        contributions_count=contrib_count,
                    )
                )

            if index + 1 == max:
                break

        return contribs

    @_gl_response_error_handler
    def get_repository_owner(self, repository_id: str):
        assert repository_id.isdigit()
        proj = self._gl.projects.get(int(repository_id))
        return GenericOwner(
            id=str(proj.namespace.id),
            path_name=proj.namespace.path,
            display_name=proj.namespace.name,
            type=(
                GenericOwnerType.Person
                if proj.namespace.kind == "user"
                else GenericOwnerType.Organization
            ),
        )

    @_gl_response_error_handler
    def list_repository_webhooks(
        self, repository_id: str
    ) -> list[GenericWebhook] | None:
        assert repository_id.isdigit()
        proj = self._gl.projects.get(int(repository_id), lazy=True)
        hooks: list[GenericWebhook] = []
        for hook in proj.hooks.list(iterator=True):
            hooks.append(
                GenericWebhook(
                    id=str(hook.id),
                    repository_id=str(hook.project_id),
                    url=hook.url,
                )
            )
        return hooks

    @_gl_response_error_handler
    def create_webhook(self, repository_id: str) -> str | None:
        assert repository_id.isdigit()
        proj = self._gl.projects.get(int(repository_id), lazy=True)

        hook_data = {
            "url": self.webhook_url,
            "token": self.factory.config.get("shared_validation_token"),
            "releases_events": True,
            "description": "Managed by {}".format(
                current_app.config.get("THEME_SITENAME", "Invenio")
            ),
        }

        resp = proj.hooks.create(hook_data)
        return str(resp.id)

    @_gl_response_error_handler
    def delete_webhook(self, repository_id: str, hook_id=None) -> bool:
        assert repository_id.isdigit()
        if hook_id is not None:
            assert hook_id.isdigit()

        proj = self._gl.projects.get(int(repository_id), lazy=True)
        if hook_id is None:
            first_valid = self.get_first_valid_webhook(repository_id)
            if first_valid is None:
                return True

            proj.hooks.delete(int(first_valid.id))
        else:
            proj.hooks.delete(int(hook_id))

        return True

    @_gl_response_error_handler
    def get_own_user(self) -> GenericUser | None:
        user = self._gl.user
        if user is None:
            return None
        return GenericUser(
            id=str(user.id),
            username=user.username,
            display_name=user.name,
        )

    def resolve_release_zipball_url(self, release_zipball_url: str) -> str | None:
        # No further resolution needs to be done for GitLab, so this is a no-op
        return release_zipball_url

    @_gl_response_error_handler
    def fetch_release_zipball(self, release_zipball_url: str, timeout: int):
        resp = self._gl.http_get(
            release_zipball_url, raw=True, streamed=True, timeout=timeout
        )
        assert isinstance(resp, requests.Response)
        with resp:
            yield resp.raw

    @_gl_response_error_handler
    def retrieve_remote_file(self, repository_id: str, tag_name: str, file_name: str):
        assert repository_id.isdigit()
        proj = self._gl.projects.get(repository_id, lazy=True)
        file = proj.files.get(file_path=file_name, ref=tag_name)
        return file.decode()

    def revoke_token(self, access_token: str):
        # TODO: GitLab implements RFC7009 for OAuth Token Revocation. We might need to do this via OAuth instead of the GitLab API.
        pass
