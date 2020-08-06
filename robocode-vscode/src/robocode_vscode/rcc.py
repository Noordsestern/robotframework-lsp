from subprocess import CalledProcessError
import sys
from typing import Optional, List, Any
import weakref

from robocode_ls_core.basic import implements, as_str
from robocode_ls_core.constants import NULL
from robocode_ls_core.protocols import IConfig, IConfigProvider
from robocode_ls_core.robotframework_log import get_logger
from robocode_vscode.protocols import (
    IRcc,
    IRccWorkspace,
    IRccActivity,
    ActionResult,
    typecheck_ircc,
    typecheck_ircc_workspace,
    typecheck_ircc_activity,
)


log = get_logger(__name__)

RCC_CLOUD_ACTIVITY_MUTEX_NAME = "rcc_cloud_activity"
RCC_CREDENTIALS_MUTEX_NAME = "rcc_credentials"


def download_rcc(location: str, force: bool = False) -> None:
    """
    Downloads rcc to the given location. Note that we don't overwrite it if it 
    already exists (unless force == True).
    
    :param location:
        The location to store the rcc executable in the filesystem.
    :param force:
        Whether we should overwrite an existing installation.
    """
    from robocode_ls_core.system_mutex import timed_acquire_mutex
    import os.path

    if not os.path.exists(location) or force:
        with timed_acquire_mutex("robocode_get_rcc", timeout=120):
            if not os.path.exists(location) or force:
                import platform
                import urllib.request

                machine = platform.machine()
                is_64 = not machine or "64" in machine

                if sys.platform == "win32":
                    if is_64:
                        url = (
                            "https://downloads.code.robocorp.com/rcc/windows64/rcc.exe"
                        )
                    else:
                        url = (
                            "https://downloads.code.robocorp.com/rcc/windows32/rcc.exe"
                        )

                elif sys.platform == "darwin":
                    url = "https://downloads.code.robocorp.com/rcc/macos64/rcc"

                else:
                    if is_64:
                        url = "https://downloads.code.robocorp.com/rcc/linux64/rcc"
                    else:
                        url = "https://downloads.code.robocorp.com/rcc/linux32/rcc"

                log.info(f"Downloading rcc from: {url} to: {location}.")
                response = urllib.request.urlopen(url)

                # Put it all in memory before writing (i.e. just write it if
                # we know we downloaded everything).
                data = response.read()

                try:
                    os.makedirs(os.path.dirname(location))
                except Exception:
                    pass  # Error expected if the parent dir already exists.

                try:
                    with open(location, "wb") as stream:
                        stream.write(data)
                    os.chmod(location, 0x744)
                except Exception:
                    log.exception(
                        "Error writing to: %s.\nParent dir exists: %s",
                        location,
                        os.path.dirname(location),
                    )
                    raise


def get_default_rcc_location() -> str:
    from robocode_vscode import get_extension_relative_path

    if sys.platform == "win32":
        location = get_extension_relative_path("bin", "rcc.exe")
    else:
        location = get_extension_relative_path("bin", "rcc")
    return location


@typecheck_ircc_activity
class RccActivity(object):
    def __init__(self, activity_id: str, activity_name: str):
        self._activity_id = activity_id
        self._activity_name = activity_name

    @property
    def activity_id(self) -> str:
        return self._activity_id

    @property
    def activity_name(self) -> str:
        return self._activity_name


@typecheck_ircc_workspace
class RccWorkspace(object):
    def __init__(self, workspace_id: str, workspace_name: str):
        self._workspace_id = workspace_id
        self._workspace_name = workspace_name

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def workspace_name(self) -> str:
        return self._workspace_name


@typecheck_ircc
class Rcc(object):
    def __init__(self, config_provider: IConfigProvider) -> None:
        self._config_provider = weakref.ref(config_provider)

        self.credentials: Optional[str] = None

    def _get_str_optional_setting(self, setting_name) -> Any:
        config_provider = self._config_provider()
        config: Optional[IConfig] = None
        if config_provider is not None:
            config = config_provider.config

        if config:
            return config.get_setting(setting_name, str, None)
        return None

    @property
    def config_location(self) -> Optional[str]:
        """
        @implements(IRcc.config_location)
        """
        # Can be set in tests to provide a different config location.
        from robocode_vscode import settings

        return self._get_str_optional_setting(settings.ROBOCODE_RCC_CONFIG_LOCATION)

    @property
    def endpoint(self) -> Optional[str]:
        """
        @implements(IRcc.endpoint)
        """
        # Can be set in tests to provide a different endpoint.
        from robocode_vscode import settings

        return self._get_str_optional_setting(settings.ROBOCODE_RCC_ENDPOINT)

    @implements(IRcc.get_rcc_location)
    def get_rcc_location(self) -> str:
        from robocode_vscode import settings
        import os.path

        rcc_location = self._get_str_optional_setting(settings.ROBOCODE_RCC_LOCATION)
        if not rcc_location:
            rcc_location = get_default_rcc_location()

        if not os.path.exists(rcc_location):
            download_rcc(rcc_location)
        return rcc_location

    def _run_rcc(
        self,
        args: List[str],
        timeout: float = 30,
        expect_ok=True,
        error_msg: str = "",
        mutex_name=None,
    ) -> ActionResult[str]:
        from robocode_ls_core.basic import build_subprocess_kwargs
        from subprocess import check_output
        from robocode_ls_core.subprocess_wrapper import subprocess

        rcc_location = self.get_rcc_location()

        cwd = None
        env = None
        kwargs: dict = build_subprocess_kwargs(cwd, env, stderr=subprocess.PIPE)
        args = [rcc_location] + args
        cmdline = " ".join([str(x) for x in args])

        try:
            if mutex_name:
                from robocode_ls_core.system_mutex import timed_acquire_mutex
            else:
                timed_acquire_mutex = NULL
            with timed_acquire_mutex(mutex_name, timeout=15):
                boutput: bytes = check_output(args, timeout=timeout, **kwargs)

        except CalledProcessError as e:
            stdout = as_str(e.stdout)
            stderr = as_str(e.stderr)
            msg = f"Error running: {cmdline}.\nStdout: {stdout}\nStderr: {stderr}"
            log.exception(msg)
            if not error_msg:
                return ActionResult(success=False, message=msg)
            else:
                additional_info = [error_msg]
                if stdout or stderr:
                    if stdout and stderr:
                        additional_info.append("\nDetails: ")
                        additional_info.append("\nStdout")
                        additional_info.append(stdout)
                        additional_info.append("\nStderr")
                        additional_info.append(stderr)

                    elif stdout:
                        additional_info.append("\nDetails: ")
                        additional_info.append(stdout)

                    elif stderr:
                        additional_info.append("\nDetails: ")
                        additional_info.append(stderr)

                return ActionResult(success=False, message="".join(additional_info))

        except Exception:
            msg = f"Error running: {args}"
            log.exception(msg)
            return ActionResult(success=False, message=msg)

        output = boutput.decode("utf-8", "replace")

        log.debug(f"Output from: {cmdline}:\n{output}")
        if expect_ok:
            if "OK." in output:
                return ActionResult(success=True, message=None, result=output)
        else:
            return ActionResult(success=True, message=None, result=output)

        return ActionResult(
            success=False, message="OK. not found in message", result=output
        )

    @implements(IRcc.get_template_names)
    def get_template_names(self) -> ActionResult[List[str]]:
        result = self._run_rcc("activity initialize -l".split())
        if not result.success:
            return ActionResult(success=False, message=result.message)

        output = result.result
        if output is None:
            return ActionResult(success=False, message="Output not available")
        templates = []
        for line in output.splitlines():
            if line.startswith("- "):
                template_name = line[2:].strip()
                templates.append(template_name)

        return ActionResult(success=True, message=None, result=sorted(templates))

    def _add_config_to_args(self, args: List[str]) -> List[str]:
        config_location = self.config_location
        if config_location:
            args.append("--config")
            args.append(config_location)
        return args

    @implements(IRcc.create_activity)
    def create_activity(self, template: str, directory: str) -> ActionResult:
        args = ["activity", "initialize", "-t", template, "-d", directory]
        args = self._add_config_to_args(args)
        return self._run_rcc(args, error_msg="Error creating activity.")

    @implements(IRcc.add_credentials)
    def add_credentials(self, credential: str) -> ActionResult:
        args = ["config", "credentials"]
        endpoint = self.endpoint
        if endpoint:
            args.append("--endpoint")
            args.append(endpoint)

        args = self._add_config_to_args(args)

        args.append(credential)

        return self._run_rcc(args, mutex_name=RCC_CREDENTIALS_MUTEX_NAME)

    @implements(IRcc.credentials_valid)
    def credentials_valid(self) -> bool:
        import json

        args = ["config", "credentials", "-j", "--verified"]
        endpoint = self.endpoint
        if endpoint:
            args.append("--endpoint")
            args.append(endpoint)

        args = self._add_config_to_args(args)

        result = self._run_rcc(
            args, expect_ok=False, mutex_name=RCC_CREDENTIALS_MUTEX_NAME
        )
        if not result.success:
            msg = f"Error checking credentials: {result.message}"
            log.critical(msg)
            return False

        output = result.result
        if not output:
            msg = f"Error. Expected to get info on credentials (found no output)."
            log.critical(msg)
            return False

        for credential in json.loads(output):
            timestamp = credential.get("verified")
            if timestamp and int(timestamp):
                return True
        # Found no valid credential
        return False

    @implements(IRcc.cloud_list_workspaces)
    def cloud_list_workspaces(self) -> ActionResult[List[IRccWorkspace]]:
        import json

        ret: List[IRccWorkspace] = []
        args = ["cloud", "workspace"]
        args = self._add_config_to_args(args)

        result = self._run_rcc(
            args, expect_ok=False, mutex_name=RCC_CLOUD_ACTIVITY_MUTEX_NAME
        )

        if not result.success:
            return ActionResult(False, result.message)

        output = result.result
        if not output:
            return ActionResult(
                False, "Error listing cloud workspaces (output not available)."
            )

        for workspace_info in json.loads(output):
            ret.append(
                RccWorkspace(
                    workspace_id=workspace_info["id"],
                    workspace_name=workspace_info["name"],
                )
            )
        return ActionResult(True, None, ret)

    @implements(IRcc.cloud_list_workspace_activities)
    def cloud_list_workspace_activities(
        self, workspace_id: str
    ) -> ActionResult[List[IRccActivity]]:
        import json

        ret: List[IRccActivity] = []
        args = ["cloud", "workspace"]
        args.extend(("--workspace", workspace_id))
        args = self._add_config_to_args(args)
        result = self._run_rcc(
            args, expect_ok=False, mutex_name=RCC_CLOUD_ACTIVITY_MUTEX_NAME
        )
        if not result.success:
            return ActionResult(False, result.message)

        output = result.result
        if not output:
            return ActionResult(
                False,
                "Error listing cloud workspace activities (output not available).",
            )

        workspace_info = json.loads(output)
        for activity_info in workspace_info["activities"]:
            ret.append(
                RccActivity(
                    activity_id=activity_info["id"], activity_name=activity_info["name"]
                )
            )
        return ActionResult(True, None, ret)

    @implements(IRcc.cloud_set_activity_contents)
    def cloud_set_activity_contents(
        self, directory: str, workspace_id: str, package_id: str
    ) -> ActionResult:
        import os.path

        assert os.path.isdir(
            directory
        ), f"Expected: {directory} to exist and be a directory."

        args = ["cloud", "push"]
        args.extend(["--directory", directory])
        args.extend(["--workspace", workspace_id])
        args.extend(["--package", package_id])

        args = self._add_config_to_args(args)
        return self._run_rcc(args, mutex_name=RCC_CLOUD_ACTIVITY_MUTEX_NAME)

    @implements(IRcc.cloud_create_activity)
    def cloud_create_activity(self, workspace_id: str, package_id: str) -> ActionResult:
        args = ["cloud", "new"]
        args.extend(["--workspace", workspace_id])
        args.extend(["--package", package_id])

        args = self._add_config_to_args(args)
        return self._run_rcc(args, mutex_name=RCC_CLOUD_ACTIVITY_MUTEX_NAME)
