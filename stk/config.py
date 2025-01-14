from __future__ import annotations

import pathlib
import re
import os
import logging
import git

from dataclasses import dataclass
from datetime import datetime
from jinja2 import Environment, StrictUndefined
from pathlib import Path
from rich.console import Console
from rich.table import Table
from sys import exc_info
from yaml import safe_load


from . import ConfigException, VERSION
from .config_file import ConfigFile
from .template_source import TemplateSource
from .basic_stack import StackReference
from .aws_config import AwsSettings

log = logging.getLogger("config")


class Config:
    @dataclass
    class InterpolationError:
        key: str
        value: str
        error: str

    @dataclass
    class CoreSettings:
        # Attributes
        stack_name: str
        environments: list = None

        # DEFAULTS are pre-interpolation values so can't set them via attributes
        DEFAULTS = {"stack_name": "{{ environment }}-{{ name.replace('/', '-') }}"}

        # stack name
        valid_stack_name = re.compile("^[a-zA-Z0-9-]+$").match

        def __post_init__(self):
            if type(self.stack_name) != str or not self.valid_stack_name(self.stack_name):
                raise ValueError(f"Stack name {self.stack_name} is invalid. Can contain only alphanumeric characters and hyphens")

    class Vars(dict):
        MAX_INTERPOLATION_DEPTH = 10

        def __init__(self, vars: dict):
            """
            We want to expand interpolated 'vars' using Jinja2.

            This is a bit different than how to expand other Jinja-value dicts because we'll need to iterate through a few
            times until all vars have been expanded.
            """
            failed_keys = self.expand(vars)

            if failed_keys:
                errors = Table("Key", "Value", "Error")
                for k, v in failed_keys.items():
                    errors.add_row(k, str(v.value), str(v.error))
                Console().log(errors)
                raise Exception(f"An error occurred processing vars: {failed_keys}")

        def expand(self, vars: dict):
            """
            Updates `self` with final interpolated values from initial dict `vars`.

            Returns dict InterpolationError for all keys that can't be expanded
            """
            env = Environment(undefined=StrictUndefined)

            interpolation_depth = 0

            errors = {}
            while interpolation_depth <= self.MAX_INTERPOLATION_DEPTH:
                interpolation_depth += 1
                unexpanded_keys = sorted(set(vars.keys()) - set(self.keys()))
                if not unexpanded_keys:
                    return None

                for key in unexpanded_keys:
                    value = vars[key]
                    try:
                        del errors[key]
                        if type(value) in [bool, dict, list, str]:
                            tpl = env.from_string(str(value))  # convert value to jinja2 template
                            result = str(tpl.render(self))
                            self[key] = safe_load(result)
                            # print(f"expanding {key}, {value}({type(value)}) -> {self[key]}({type(self[key])})")
                        else:
                            # print(f"skipping {key}, type={type(value)}")
                            self[key] = value  # Don't try and process this value as Jinja template
                        del vars[key]
                    except Exception:
                        errors[key] = Config.InterpolationError(key, value, exc_info()[1])

            return errors

    class InterpolatedDict(dict):
        def __init__(self, object: dict, vars: dict):
            # Handle loading from empty YAML file (results in None), or a 'config group' (e.g. params)
            # not being present - which is okay.
            if not object:
                return

            if type(object) != dict:
                raise Exception(object)

            env = Environment(undefined=StrictUndefined)

            for k, v in object.items():
                try:
                    if v == None:
                        self[k] = None
                    else:
                        value = env.from_string(str(v)).render(vars)
                        parsed_value = safe_load(value)
                        if parsed_value != None:
                            self[k] = parsed_value
                except Exception as ex:
                    raise (Exception(f"Unable to process {k}, value={object[k]} : {ex}"))

    class Tags(InterpolatedDict):
        def to_list(self, extra_attributes={}):
            ret_val = []
            for k, v in self.items():
                ret_val.append({"Key": str(k), "Value": str(v), **extra_attributes})
            return ret_val

    class StackRefs:
        DEFAULTS = {"stack_name": "{{ environment }}-{{ name }}", "optional": False}

        def __init__(self, stack_refs: dict, config: Config):
            self.config = config
            self.refs = stack_refs
            log.debug("defined refs: %s" % self.refs)

        def __contains__(self, name: str) -> bool:
            return name in self.stacks()

        def __getitem__(self, name: str) -> str:
            return self.stack(name)

        def exists(self, name: str):
            return self[name].exists()

        def output(self, name: str, output_name: str) -> str:
            log.info(f"getting output {output_name} from stack {name}")
            stack = self.stack(name)
            if not stack.exists():
                if stack.optional:
                    log.info("stack does not exist, but is optional")
                    return None
                raise Exception(f"Stack config.refs[{name}] ({stack.name}) does not exist, but is required")

            return stack.output(output_name)

        def stack(self, name: str) -> StackReference:
            """
            Returns stack object, or None if stack is optional but is not found
            """
            stacks = self.stacks()
            if name not in stacks:
                stack_names = sorted(self.refs.keys())
                raise Exception(f"Attempt to access stack {name}, but it's not defined in config.refs - only {', '.join(stack_names)} are defined")

            return stacks.get(name)

        @dataclass
        class StackRefOpts:
            stack_name: str
            optional: bool

        class OptionalStackReference(StackReference):
            def __init__(self, aws: AwsSettings, name: str, optional: bool):
                super().__init__(aws=aws, name=name)
                self.optional = optional

            def __getitem__(self, name: str) -> str:
                return self.output(name)

            def describe_stack(self):
                """
                For references, cache the describe_stack - we're not expecting
                it to change.
                """
                if not hasattr(self, "_describe_stack_result"):
                    self._describe_stack_result = super().describe_stack()
                return self._describe_stack_result

        def stacks(self) -> dict:
            if not hasattr(self, "_stacks"):
                # _stacks is dict of {name => StackReference() for each named stack. This includes
                # stacks that don't exist.
                self._stacks = dict()
                for name, cfg in self.refs.items():
                    if name == "environment":
                        continue

                    if not cfg:
                        cfg = {}

                    if not issubclass(type(cfg), dict):
                        print(f"{name} is not a valid stack reference definition (from {self.refs})")
                        exit(-1)

                    # Try building dict of options. This can fail if interpolating incorrect variable or
                    try:
                        final_opts = Config.InterpolatedDict({**self.DEFAULTS, **cfg}, {"environment": self.config.environment, "name": name.replace("_", "-")})
                    except Exception as ex:
                        print(f"Unable to process settings for stack reference {name} -> {cfg} (from {self.refs})")
                        exit(-1)

                    try:
                        log.info(f"stack reference {name}: {final_opts}")
                        opts = self.StackRefOpts(**final_opts)
                    except Exception as ex:
                        log.exception(f"Invalid configuration for stack.refs '{name}' {self.refs}: {ex}", exc_info=ex)
                        raise

                    self._stacks[name] = self.OptionalStackReference(aws=self.config.aws, name=opts.stack_name, optional=opts.optional)

            return self._stacks

    @dataclass
    class DeployMetadata:
        def __init__(self, config_path: str, template_source: TemplateSource):
            # Timestamp in UTC
            self.timestamp = datetime.utcnow().strftime("%Y-%m-%d-%H:%M:%S%Z")

            # STK version
            self.deployed_with = f"stk-{VERSION}"

            if template_source.repo:
                self.template = "/".join([template_source.repo, template_source.root, template_source.name])
            else:
                self.template = "/".join([template_source.root, template_source.name])

            # Config git HEAD state
            try:
                config_head = git.Repo(config_path).head
                self.config_sha = str(config_head.commit.hexsha)
                self.config_ref = str(config_head.reference)
            except Exception as ex:
                log.warning("Unable to retrieve git info for config project", exc_info=ex)
                self.config_sha = "?"
                self.config_ref = "?"

            self.template_sha = "?"
            self.template_ref = "?"

    def __init__(
        self,
        name: str,
        environment: str,
        config_path: str,
        template_path: str = None,
        var_overrides: dict = {},
        param_overrides: dict = {},
    ):
        # While we should just receive `name`, we may be be passed
        name = str(Path(name).with_suffix(""))
        self.name = name
        self.environment = environment
        self.config_path = config_path

        try:
            cfg = ConfigFile(filename=name, config_dir=self.config_path)
        except FileNotFoundError as err:
            raise Exception(f"Configuration file {name} not found in {config_path}: {err}")

        # Validate specified environment is defined in the top-level config file
        if environment not in cfg.environments():
            raise ConfigException(f"Environment {environment} is not a valid environment for {cfg.filename}. Only {cfg.environments()} permitted.")

        # Load top-level config file and all included configs
        includes = cfg.load_includes()

        # Most other config supports pulling stuff from AWS, so initialize this first
        try:
            aws_settings = self.InterpolatedDict(includes.fetch_dict("aws", environment), {"environ": os.environ, "environment": environment})
            self.aws = AwsSettings(**aws_settings)
            self.aws.get_account_id()  # force retrieval of account_id
        except TypeError as ex:
            raise Exception(f"Unable to parse aws settings: have {aws_settings}: {ex}")

        default_vars = {
            "__config_dir": pathlib.Path(config_path),
            "account_id": self.aws.account_id,
            "aws_region": self.aws.region,
            "cfn_bucket": self.aws.cfn_bucket,
            "environ": os.environ,
            "environment": environment,
            "name": name,
        }

        # Core settings impact the behavior of 'stk' - e.g. stack name, valid environments
        # etc.
        self.core = self.CoreSettings(
            **self.InterpolatedDict(
                includes.fetch_dict("core", environment, self.CoreSettings.DEFAULTS),
                default_vars,
            )
        )

        # Ugly hack. Need to come up with something better after I've had a coffee
        default_vars["stack_name"] = self.core.stack_name

        # Stack 'refs' object references external stacks. They are intended to be resolved by 'vars'/'params' so need to be
        # loaded first
        try:
            refs = self.InterpolatedDict(includes.fetch_dict("refs", environment), {"environment": environment})
            self.refs = self.StackRefs(refs, self)
        except Exception as ex:
            raise Exception("Unable to parse stack refs (refs:). have {refs}: {ex}")
        default_vars["refs"] = self.refs

        self.helpers = list(includes.fetch_set("helpers", environment))

        pre_vars = includes.fetch_dict("vars", environment, default_vars)
        pre_vars.update(var_overrides)
        self.vars = self.Vars(pre_vars)

        params = includes.fetch_dict("params", environment)
        params.update(param_overrides)
        self.params = self.InterpolatedDict(params, self.vars)
        self.vars["params"] = self.params

        template_source = self.InterpolatedDict(
            includes.fetch_dict(
                "template",
                environment,
                {"name": name.replace("/", "-"), "root": None},
            ),
            self.vars,
        )

        # I'm not happy about this
        if template_source["root"] == None:
            if "repo" in template_source and template_source["repo"]:
                # git repo, we default "root" to / - i.e. relevant to git root
                template_source["root"] = "/"
            else:
                # filesystem repo, we default "root" to provided template_path (--template-path args)
                # mostly useful for tests
                template_source["root"] = template_path

        self.template_source = TemplateSource(**template_source)

        # Deploy metadata is used to track deploys back to version controlled config/templates.
        self.vars["deploy"] = self.DeployMetadata(config_path=config_path, template_source=self.template_source)

        self.tags = self.Tags(includes.fetch_dict("tags", environment), self.vars)

        # perform final linting/validation
        includes.validate(self)

    def var(self, name):
        return self.vars.get(name)

    def param(self, name):
        return self.params.get(name)
