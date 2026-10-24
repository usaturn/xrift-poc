"""#578: Vertex launchers change only their process tree, never saved auth.

Pi is the interactive/network boundary. The fake bwrap consumes the real FD
payload without mounting anything; real bwrap tests exercise namespace inheritance.
All auth and model-key fixtures below are synthetic, never the user's config.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[3] / ".devcontainer"
LAUNCHERS = ("pigemf", "pigemp", "pigema")


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home with spaces"
    bin_dir = tmp_path / "bin"
    cwd = tmp_path / "working directory"
    bin_dir.mkdir()
    cwd.mkdir()
    for launcher in LAUNCHERS:
        config = home / f".pi/agent/extensions/subagent.d/{launcher}/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"superagents":{"modelTiers":{}}}\n')
    # Match the installed npm Pi layout, including the bundled metadata format.
    real_pi = shutil.which("pi")
    assert real_pi is not None
    package = tmp_path / "pi-package"
    data = package / "node_modules/@earendil-works/pi-ai/dist/providers/data/google-vertex.json"
    data.parent.mkdir(parents=True)
    installed_package = Path(real_pi).resolve().parents[2]
    shutil.copyfile(installed_package / data.relative_to(package), data)
    (package / "package.json").write_text('{"name":"@earendil-works/pi-coding-agent"}')
    cli = package / "dist/bundle/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(), 'home':os.environ['HOME'],\n"
        "'project':os.environ.get('GOOGLE_CLOUD_PROJECT'), 'location':os.environ.get('GOOGLE_CLOUD_LOCATION'),\n"
        "'adc':os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'), 'other':os.environ.get('TEST_UNRELATED'),\n"
        "'cloud_key_present':'GOOGLE_CLOUD_API_KEY' in os.environ}))\n"
        "sys.exit(int(os.environ.get('TEST_PI_EXIT', '0')))\n"
    )
    cli.chmod(0o755)
    (bin_dir / "pi").symlink_to(cli)
    bwrap = bin_dir / "bwrap"
    bwrap.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if '--ro-bind-data' in args:\n"
        "    at = args.index('--ro-bind-data')\n"
        "    with os.fdopen(int(args[at+1])) as stream: stream.read()\n"
        "command = args[args.index('--')+1:]\n"
        "os.execvp(command[0], command)\n"
    )
    bwrap.chmod(0o755)
    # These commands must never be invoked by the launchers.
    gcloud = bin_dir / "gcloud"
    gcloud.write_text('#!/bin/sh\necho "gcloud must not run" >&2\nexit 99\n')
    gcloud.chmod(0o755)
    return home, bin_dir, cwd


def _env(sandbox: tuple[Path, Path, Path]) -> dict[str, str]:
    home, bin_dir, _ = sandbox
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for key in (
        "PI_CODING_AGENT_DIR",
        "PI_SUPERAGENTS_LAUNCHER_DRY_RUN",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_API_KEY",
    ):
        env.pop(key, None)
    return env


def _run(
    sandbox: tuple[Path, Path, Path], launcher: str, args: tuple[str, ...] = (), *, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["zsh", str(DEVCONTAINER / launcher), *args],
        cwd=sandbox[2],
        env={**_env(sandbox), **(extra or {})},
        capture_output=True,
        text=True,
        timeout=15,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    return cast(dict[str, object], json.loads(result.stdout))


def _launcher_model(sandbox: tuple[Path, Path, Path], launcher: str) -> str:
    """dry-run の argv から、ランチャーが Pi に渡すモデル ID を読む（モデル名はテストに写さない）。"""
    result = _run(sandbox, launcher, extra={"PI_SUPERAGENTS_LAUNCHER_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    return argv[argv.index("--model") + 1]


# Break caught: dropping quoting/exec, changing HOME/cwd, or adding an ADC check
# prevents the real launcher from reaching Pi with the original request.
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_launch_reaches_pi_without_adc_preserving_args_cwd_environment_and_exit(
    sandbox: tuple[Path, Path, Path], launcher: str
) -> None:
    args = ("--session", "session with spaces.jsonl", "", "--", "a 'quoted' message\nnext line")
    result = _run(sandbox, launcher, args, extra={"TEST_UNRELATED": "keep me", "TEST_PI_EXIT": "37"})
    assert result.returncode == 37, result.stderr
    payload = _payload(result)
    launched = cast(list[str], payload["args"])
    # モデル ID の値はランチャーの設定なので見ない（存在は起動前の catalog 確認が保証する）。
    assert launched[:5] == ["--tui-mode", "regular", "--provider", "google-vertex", "--model"]
    assert launched[5]
    assert launched[6:] == ["--thinking", "high", *args]
    assert payload["cwd"] == str(sandbox[2])
    assert payload["home"] == str(sandbox[0])
    assert payload["other"] == "keep me"
    assert payload["adc"] is None


# Break caught: wrong fallback branch, clobbered supplied defaults/ADC, or leaked
# cloud API key into Pi's process tree. Expectations are independent inputs.
@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize(
    ("supplied", "project", "location"),
    [
        ({}, "hobby-372711", "global"),
        ({"GCLOUD_PROJECT": "legacy-project"}, "legacy-project", "global"),
        (
            {
                "GOOGLE_CLOUD_PROJECT": "primary-project",
                "GCLOUD_PROJECT": "legacy-project",
                "GOOGLE_CLOUD_LOCATION": "custom-location",
            },
            "primary-project",
            "custom-location",
        ),
        (
            {"GOOGLE_CLOUD_PROJECT": "", "GCLOUD_PROJECT": "fallback-project", "GOOGLE_CLOUD_LOCATION": ""},
            "fallback-project",
            "global",
        ),
    ],
)
def test_cloud_defaults_precedence_and_api_key_removal(
    sandbox: tuple[Path, Path, Path], launcher: str, supplied: dict[str, str], project: str, location: str
) -> None:
    result = _run(
        sandbox,
        launcher,
        extra={
            **supplied,
            "GOOGLE_APPLICATION_CREDENTIALS": "missing synthetic ADC path.json",
            "GOOGLE_CLOUD_API_KEY": "synthetic-env-secret",
        },
    )
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    assert payload["project"] == project
    assert payload["location"] == location
    assert payload["adc"] == "missing synthetic ADC path.json"
    assert payload["cloud_key_present"] is False
    assert "synthetic-env-secret" not in result.stdout + result.stderr


def _real_namespace_probe(sandbox: tuple[Path, Path, Path]) -> None:
    """Replace only the TUI/API process, keeping real mounts and child inheritance."""
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    (sandbox[1] / "bwrap").unlink()
    (sandbox[1] / "bwrap").symlink_to(bwrap)
    real_pi = shutil.which("pi")
    assert real_pi is not None
    native_package = Path(real_pi).resolve().parents[2]
    probe = sandbox[2].parent / "native-auth-probe.mjs"
    probe.write_text(
        "import { pathToFileURL } from 'node:url';\n"
        "import { join, dirname } from 'node:path';\n"
        "const [packageDir, authFile] = process.argv.slice(2);\n"
        "const load = (file) => import(pathToFileURL(join(packageDir, file)).href);\n"
        "const { AuthStorage } = await load('dist/core/auth-storage.js');\n"
        "const { ModelConfig } = await load('dist/core/model-config.js');\n"
        "const { composeModelProvider } = await load('dist/core/provider-composer.js');\n"
        "const { googleVertexProvider } = await load(\n"
        "    'node_modules/@earendil-works/pi-ai/dist/providers/google-vertex.js');\n"
        "const config = await ModelConfig.load(join(dirname(authFile), 'models.json'));\n"
        "if (config.getError()) throw new Error('invalid synthetic model fixture');\n"
        "const credential = await AuthStorage.create(authFile).read('google-vertex');\n"
        "const provider = composeModelProvider('google-vertex', googleVertexProvider(), config);\n"
        "const resolved = await provider.auth.apiKey.resolve({credential,\n"
        "    ctx: { env: async (name) => process.env[name],\n"
        "        fileExists: async () => { throw new Error('must not probe ADC'); } },\n"
        "    signal: new AbortController().signal});\n"
        "console.log(JSON.stringify({\n"
        "    native_key_is_adc_marker: resolved?.auth.apiKey === 'gcp-vertex-credentials'}));\n"
    )
    cli = (sandbox[1] / "pi").resolve()
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "directory = os.environ.get('PI_CODING_AGENT_DIR') or os.environ['HOME']+'/.pi/agent'\n"
        "auth_path = Path(directory).expanduser().resolve() / 'auth.json'\n"
        "auth = json.loads(auth_path.read_text()) if auth_path.exists() else {}\n"
        "marker = {'type':'api_key', 'key':'gcp-vertex-credentials'}\n"
        "other_key = auth.get('other-provider', {}).get('key')\n"
        "env_contains_other_key = bool(other_key) and any(other_key in value for value in os.environ.values())\n"
        "if os.environ.get('TEST_WRITE_STATE') == '1':\n"
        "    (auth_path.parent / 'sessions').mkdir(exist_ok=True)\n"
        "    (auth_path.parent / 'sessions/first.jsonl').write_text('first conversation\\n')\n"
        "    (auth_path.parent / 'settings.json').write_text('{\"theme\":\"saved\"}')\n"
        "    (auth_path.parent / 'settings.json.lock').mkdir(exist_ok=True)\n"
        "try:\n"
        "    with auth_path.open('r+') as stream: stream.write('')\n"
        "    readonly = False\n"
        "except OSError: readonly = True\n"
        "config = Path(os.environ['HOME'])/'.pi/agent/extensions/subagent/config.json'\n"
        "tiers = json.loads(config.read_text())['superagents']['modelTiers'] if config.exists() else {}\n"
        "payload = {'vertex_is_adc_marker':auth.get('google-vertex')==marker,\n"
        "'other_auth_preserved':auth.get('other-provider')=={'type':'api_key','key':'synthetic-other-secret'},\n"
        "'env_contains_other_key':env_contains_other_key,\n"
        "'cloud_key_present':'GOOGLE_CLOUD_API_KEY' in os.environ, 'readonly':readonly, 'tiers':tiers}\n"
        f"native = subprocess.run(['node', {str(probe)!r}, {str(native_package)!r}, str(auth_path)],\n"
        "    check=True, capture_output=True, text=True)\n"
        "payload.update(json.loads(native.stdout))\n"
        "if '--test-child' not in sys.argv:\n"
        "    child = subprocess.run([sys.executable, __file__, '--test-child'],\n"
        "        cwd=os.environ.get('TEST_CHILD_CWD'), check=True, capture_output=True, text=True)\n"
        "    payload['child'] = json.loads(child.stdout)\n"
        "print(json.dumps(payload))\n"
    )


# Break caught: mounting the HOME auth path instead of the actual agent dir,
# losing non-OAuth providers, retaining Vertex keys/unneeded saved env, writable overlay,
# or overwriting saved auth while preparing a missing mount target.
@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("agent_dir", (None, "~/.custom agent", "relative agent", "absolute", "~"))
@pytest.mark.parametrize("stored_auth", (True, False))
def test_real_namespace_masks_auth_for_parent_and_child_preserving_saved_files(
    sandbox: tuple[Path, Path, Path], launcher: str, agent_dir: str | None, stored_auth: bool
) -> None:
    """Overlay を export しても起動は成功し、他 provider の key が子の環境へ黙って漏れる。"""
    home, _, cwd = sandbox
    directory = home / ".pi/agent"
    if agent_dir is not None:
        if agent_dir == "absolute":
            agent_dir = str(cwd / "absolute agent")
        expanded = agent_dir.replace("~", str(home), 1) if agent_dir.startswith("~") else agent_dir
        directory = (cwd / expanded).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    auth = directory / "auth.json"
    if stored_auth:
        auth.write_text(
            json.dumps(
                {
                    "google-vertex": {
                        "type": "api_key",
                        "key": "synthetic-stored-secret",
                        "env": {
                            "GOOGLE_CLOUD_API_KEY": "synthetic-stored-env-secret",
                            "GOOGLE_CLOUD_PROJECT": "wrong-project",
                        },
                    },
                    "other-provider": {"type": "api_key", "key": "synthetic-other-secret"},
                }
            )
        )
    models = directory / "models.json"
    models.write_text('{"providers":{"google-vertex":{"apiKey":"synthetic-model-secret"}}}')
    shared = home / ".pi/agent/extensions/subagent/config.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text('{"superagents":{"modelTiers":{"shared":"must not be used"}}}')
    config = home / f".pi/agent/extensions/subagent.d/{launcher}/config.json"
    config.write_text('{"superagents":{"modelTiers":{"cheap":{"model":"distinct/tier","thinking":"high"}}}}')
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    before.update({path: path.read_bytes() for path in directory.rglob("*") if path.is_file()})
    _real_namespace_probe(sandbox)
    result = _run(
        sandbox,
        launcher,
        extra={
            **({"PI_CODING_AGENT_DIR": agent_dir} if agent_dir is not None else {}),
            "GOOGLE_CLOUD_API_KEY": "synthetic-env-secret",
        },
    )
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    for observed in (payload, payload["child"]):
        assert isinstance(observed, dict)
        assert observed["vertex_is_adc_marker"] is True
        assert observed["native_key_is_adc_marker"] is True
        assert observed["other_auth_preserved"] == stored_auth
        assert observed["env_contains_other_key"] is False
        assert observed["readonly"] is True
        assert observed["cloud_key_present"] is False
        assert observed["tiers"] == {"cheap": {"model": "distinct/tier", "thinking": "high"}}
    assert {path: path.read_bytes() for path in before} == before
    if not stored_auth:
        assert json.loads(auth.read_text()) == {}
        assert auth.stat().st_mode & 0o777 == 0o600
    assert not list(directory.glob(".pi-vertex-mount-*"))
    assert "secret" not in result.stdout + result.stderr


# Break caught: masking the whole agent directory drops fresh non-auth state
# and puts shared settings locks in a private namespace rather than on the host.
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_nonauth_state_and_settings_lock_remain_host_backed(sandbox: tuple[Path, Path, Path], launcher: str) -> None:
    directory = sandbox[0] / ".pi/agent"
    auth = directory / "auth.json"
    auth.write_text("{}")
    _real_namespace_probe(sandbox)
    result = _run(sandbox, launcher, extra={"TEST_WRITE_STATE": "1"})
    assert result.returncode == 0, result.stderr
    assert (directory / "sessions/first.jsonl").read_text() == "first conversation\n"
    assert json.loads((directory / "settings.json").read_text()) == {"theme": "saved"}
    assert (directory / "settings.json.lock").is_dir()
    assert auth.read_text() == "{}"


# Break caught: competing initializers replace saved auth or leave invalid JSON,
# world-readable state, or staging files behind during simultaneous starts.
def test_concurrent_launchers_initialize_only_empty_missing_auth(sandbox: tuple[Path, Path, Path]) -> None:
    auth = sandbox[0] / ".pi/agent/auth.json"

    def launch(launcher: str) -> subprocess.CompletedProcess[str]:
        return _run(sandbox, launcher)

    with ThreadPoolExecutor(max_workers=len(LAUNCHERS)) as pool:
        results = list(pool.map(launch, LAUNCHERS))
    for result in results:
        assert result.returncode == 0, result.stderr
    assert json.loads(auth.read_text()) == {}
    assert auth.stat().st_mode & 0o777 == 0o600
    assert not list(auth.parent.glob(".pi-vertex-mount-*"))


# Break caught: parsing or preparing an auth overlay before dry-run returns.
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_dry_run_is_argv_only_and_never_reads_or_creates_auth(sandbox: tuple[Path, Path, Path], launcher: str) -> None:
    home, _, cwd = sandbox
    directory = cwd / "uncreated agent"
    result = _run(
        sandbox,
        launcher,
        extra={
            "PI_CODING_AGENT_DIR": str(directory),
            "PI_SUPERAGENTS_LAUNCHER_DRY_RUN": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert not directory.exists()
    assert not (home / ".pi/agent/extensions/subagent").exists()
    args = result.stdout.splitlines()
    assert args[0] == "bwrap"
    assert "--ro-bind-data" in args
    assert args[args.index("--ro-bind-data") + 2] == str(directory / "auth.json")
    # A FIFO makes accidental configuration reads block instead of silently pass.
    directory.mkdir()
    os.mkfifo(directory / "auth.json")
    second = _run(
        sandbox,
        launcher,
        extra={
            "PI_CODING_AGENT_DIR": str(directory),
            "PI_SUPERAGENTS_LAUNCHER_DRY_RUN": "1",
        },
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout == result.stdout


# Break caught: falling back on {}, logging parser details containing auth,
# or launching Pi after malformed/unreadable saved config.
@pytest.mark.parametrize("content", ('{"synthetic-secret":', "[]", "null", '"synthetic-secret"'))
def test_invalid_auth_configuration_fails_closed_without_secret_output(
    sandbox: tuple[Path, Path, Path], content: str
) -> None:
    auth = sandbox[0] / ".pi/agent/auth.json"
    auth.write_text(content)
    result = _run(sandbox, "pigemf")
    assert result.returncode != 0
    assert "auth.json" in result.stderr
    assert not result.stdout
    assert "synthetic-secret" not in result.stderr
    assert auth.read_text() == content


def _catalog(sandbox: tuple[Path, Path, Path]) -> Path:
    package = (sandbox[1] / "pi").resolve().parents[2]
    return package / "node_modules/@earendil-works/pi-ai/dist/providers/data/google-vertex.json"


def _used_vertex_models(sandbox: tuple[Path, Path, Path], launcher: str) -> list[str]:
    """親 Pi の --model と、リポジトリの tier が使う google-vertex のモデル ID（値はテストに写さない）。"""
    tiers = json.loads((DEVCONTAINER / f"pi-superagents/{launcher}.json").read_text())["superagents"]["modelTiers"]
    used = [_launcher_model(sandbox, launcher)]
    for tier in tiers.values():
        provider, _, model = tier["model"].partition("/")
        if provider == "google-vertex" and model not in used:
            used.append(model)
    return used


# Break caught: allowing Pi fuzzy matching/custom metadata when an exact ID is
# absent, checking only the parent model while a child tier model is missing,
# or invoking an auth-aware Pi CLI command for the offline guard.
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_missing_any_used_vertex_model_stops_before_pi_and_auth_reads(
    sandbox: tuple[Path, Path, Path], launcher: str
) -> None:
    catalog = _catalog(sandbox)
    original = catalog.read_text()
    # No credential file or ADC discovery is allowed to gate this failure.
    os.mkfifo(sandbox[0] / ".pi/agent/auth.json")
    for model in _used_vertex_models(sandbox, launcher):
        data = json.loads(original)
        del data["google-vertex"][f"chat:{model}"]
        catalog.write_text(json.dumps(data))
        result = _run(sandbox, launcher)
        assert result.returncode != 0, model
        assert "catalog" in result.stderr.lower(), model
        assert not result.stdout, model


# Break caught: requiring a Vertex model the launcher and its tiers never use,
# which stops a working launcher whenever Pi drops an unrelated catalog entry.
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_catalog_with_only_used_vertex_models_reaches_pi(sandbox: tuple[Path, Path, Path], launcher: str) -> None:
    catalog = _catalog(sandbox)
    used = {f"chat:{model}" for model in _used_vertex_models(sandbox, launcher)}
    data = json.loads(catalog.read_text())
    data["google-vertex"] = {key: entry for key, entry in data["google-vertex"].items() if key in used}
    catalog.write_text(json.dumps(data))
    result = _run(sandbox, launcher)
    assert result.returncode == 0, result.stderr
    assert _payload(result)["args"]


def _native_namespace_probe(sandbox: tuple[Path, Path, Path], body: str) -> None:
    """Exercise native auth consumers in both processes; replace only network boundaries."""
    bwrap = shutil.which("bwrap")
    real_pi = shutil.which("pi")
    assert bwrap is not None and real_pi is not None
    (sandbox[1] / "bwrap").unlink()
    (sandbox[1] / "bwrap").symlink_to(bwrap)
    package = Path(real_pi).resolve().parents[2]
    probe = sandbox[2].parent / "native-request-probe.mjs"
    probe.write_text(
        """
import { pathToFileURL, fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
const [packageDir] = process.argv.slice(2);
// Never consult real ambient credentials. The probe is online only through fake fetch.
const allowed = new Set(['HOME', 'PATH', 'PI_CODING_AGENT_DIR',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT',
    'GCLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION']);
for (const name of Object.keys(process.env)) {
    if (!allowed.has(name)) delete process.env[name];
}
const load = file => import(pathToFileURL(join(packageDir, file)).href);
const authFile = join(process.env.PI_CODING_AGENT_DIR, 'auth.json');
const { AuthStorage } = await load('dist/core/auth-storage.js');
const storage = AuthStorage.create(authFile);
"""
        + body
        + """
if (!process.argv.includes('--test-child')) {
    observation.child = JSON.parse(execFileSync(process.execPath,
        [fileURLToPath(import.meta.url), packageDir, ...process.argv.slice(3), '--test-child'],
        {cwd: '/', env: process.env, encoding: 'utf8', timeout: 10000}));
}
console.log(JSON.stringify(observation));
"""
    )
    cli = (sandbox[1] / "pi").resolve()
    cli.write_text("#!/bin/sh\nexec " + shlex.join(["node", str(probe), str(package)]) + ' "$@"\n')


# Break caught: retaining OAuth in a readonly snapshot lets native startup/request
# auth rotate tokens whose replacements cannot be saved, including in child Pi.
@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("expired", (True, False))
def test_native_oauth_refresh_and_request_do_not_consume_saved_tokens(
    sandbox: tuple[Path, Path, Path], launcher: str, expired: bool
) -> None:
    auth = sandbox[0] / ".pi/agent/auth.json"
    oauth = {
        "type": "oauth",
        "access": "synthetic-old-access",
        "refresh": "synthetic-old-refresh",
        "expires": 1000 if expired else 4102444800000,
    }
    auth.write_text(
        json.dumps(
            {
                "xai": oauth,
                "openai-codex": oauth,
                "custom-oauth": oauth,
                "other-provider": {"type": "api_key", "key": "synthetic-other-secret"},
            }
        )
    )
    before = auth.read_bytes()
    _native_namespace_probe(
        sandbox,
        """
let refreshCalls = 0;
globalThis.fetch = async input => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (['https://auth.x.ai/oauth2/token', 'https://auth.openai.com/oauth/token'].includes(url)) {
        refreshCalls++;
        const access = 'synthetic.' + Buffer.from(JSON.stringify({
            'https://api.openai.com/auth': {chatgpt_account_id: 'synthetic-account'}
        })).toString('base64') + '.synthetic';
        return new Response(JSON.stringify({access_token: access,
            refresh_token: 'synthetic-new-refresh', expires_in: 3600}),
            {status: 200, headers: {'content-type': 'application/json'}});
    }
    return new Response('{}', {status: 404});
};
const { ModelRuntime } = await load('dist/core/model-runtime.js');
const runtime = await ModelRuntime.create({allowModelNetwork: false, refreshOnCreate: false});
const refreshed = await runtime.refresh({signal: AbortSignal.timeout(5000)});
const providers = ['xai', 'openai-codex'];
let requestAuthAvailable = false;
let requestAuthFailed = false;
for (const provider of providers) {
    const model = runtime.models.getModels().find(model => model.provider === provider);
    if (!model) throw new Error('native OAuth model missing');
    try {
        const resolved = await runtime.models.getAuth(model);
        requestAuthAvailable = requestAuthAvailable || !!resolved;
    } catch {
        requestAuthFailed = true;
    }
}
const observation = {
    refreshCalls, requestAuthAvailable, requestAuthFailed,
    refreshErrors: providers.filter(provider => refreshed.errors.has(provider)),
    oauthVisible: (await storage.list()).some(entry => entry.type === 'oauth'),
    otherKeyPreserved: (await storage.read('other-provider'))?.key === 'synthetic-other-secret'
};
""",
    )
    result = _run(sandbox, launcher)
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    for observed in (payload, payload["child"]):
        assert isinstance(observed, dict)
        assert observed["refreshCalls"] == 0
        assert observed["refreshErrors"] == []
        assert observed["requestAuthAvailable"] is False
        assert observed["requestAuthFailed"] is False
        assert observed["oauthVisible"] is False
        assert observed["otherKeyPreserved"] is True
    assert auth.read_bytes() == before
    assert "synthetic-old" not in result.stdout + result.stderr
    assert "synthetic-new" not in result.stdout + result.stderr
    assert "synthetic-other-secret" not in result.stdout + result.stderr


# Break caught: discarding a saved ADC path, leaving it only in marker.env (which
# native resolution does not forward), or overriding an explicit process path.
@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("stored_key", (True, False))
@pytest.mark.parametrize("supplied", (None, "", "explicit"))
@pytest.mark.parametrize("saved_name", (None, "saved ADC with spaces.json", "saved 'quoted'\nADC.json\n"))
def test_native_vertex_sdk_receives_saved_or_explicit_adc_path(
    sandbox: tuple[Path, Path, Path], launcher: str, stored_key: bool, supplied: str | None, saved_name: str | None
) -> None:
    directory = sandbox[0] / ".pi/agent"
    saved_path = str(sandbox[2] / saved_name) if saved_name is not None else None
    explicit_path = str(sandbox[2] / "different explicit ADC.json")
    credential: dict[str, object] = {
        "type": "api_key",
        "env": {
            "GOOGLE_CLOUD_API_KEY": "synthetic-saved-env-secret",
            "GOOGLE_CLOUD_PROJECT": "saved-project-must-not-win",
            "GOOGLE_CLOUD_LOCATION": "saved-location-must-not-win",
            **({"GOOGLE_APPLICATION_CREDENTIALS": saved_path} if saved_path is not None else {}),
        },
    }
    if stored_key:
        credential["key"] = "synthetic-saved-key-secret"
    auth = directory / "auth.json"
    auth.write_text(json.dumps({"google-vertex": credential}))
    (directory / "models.json").write_text('{"providers":{"google-vertex":{"apiKey":"synthetic-model-secret"}}}')
    before = auth.read_bytes()
    _native_namespace_probe(
        sandbox,
        """
const { registerHooks } = await import('node:module');
const sdkUrl = pathToFileURL(join(packageDir, 'node_modules/@google/genai/dist/node/index.mjs')).href;
const wrapper = `export * from ${JSON.stringify(sdkUrl)};
import { GoogleGenAI as RealClient } from ${JSON.stringify(sdkUrl)};
export class GoogleGenAI extends RealClient {
    constructor(options) {
        super(options);
        globalThis.sdkOptions = {
            keyFilename: options.googleAuthOptions?.keyFilename ?? null,
            apiKeyPresent: !!options.apiKey, project: options.project, location: options.location
        };
        this.models.generateContentStream = async () => (async function* () {
            yield {candidates: [{content: {parts: [{text: 'synthetic reply'}]}, finishReason: 'STOP'}],
                usageMetadata: {promptTokenCount: 1, candidatesTokenCount: 1, totalTokenCount: 2}};
        })();
    }
}`;
registerHooks({resolve(specifier, context, next) {
    if (specifier === '@google/genai') {
        return {url: 'data:text/javascript,' + encodeURIComponent(wrapper), shortCircuit: true};
    }
    return next(specifier, context);
}});
globalThis.fetch = async () => {throw new Error('external network forbidden');};
const { ModelConfig } = await load('dist/core/model-config.js');
const { composeModelProvider } = await load('dist/core/provider-composer.js');
const { createModels } = await load('node_modules/@earendil-works/pi-ai/dist/models.js');
const { googleVertexProvider } = await load('node_modules/@earendil-works/pi-ai/dist/providers/google-vertex.js');
const config = await ModelConfig.load(join(process.env.PI_CODING_AGENT_DIR, 'models.json'));
const provider = composeModelProvider('google-vertex', googleVertexProvider(), config);
const models = createModels({credentials: storage, authContext: {
    env: async name => process.env[name],
    fileExists: async () => {throw new Error('launcher must not probe ADC');}
}});
models.setProvider(provider);
const modelId = process.argv[process.argv.indexOf('--model') + 1];
const model = provider.getModels().find(model => model.id === modelId);
if (!model) throw new Error('native Vertex model missing');
const resolution = await models.getAuth(model);
const response = await provider.streamSimple(model,
    {messages: [{role: 'user', content: 'synthetic probe', timestamp: 0}]},
    {...resolution.auth, env: resolution.env, reasoning: 'high'}).result();
const observation = {sdk: globalThis.sdkOptions, stopReason: response.stopReason};
""",
    )
    extra = {"GOOGLE_CLOUD_API_KEY": "synthetic-env-secret"}
    if supplied is not None:
        extra["GOOGLE_APPLICATION_CREDENTIALS"] = explicit_path if supplied == "explicit" else supplied
    result = _run(sandbox, launcher, extra=extra)
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    expected_path = explicit_path if supplied == "explicit" else saved_path
    for observed in (payload, payload["child"]):
        assert isinstance(observed, dict)
        assert observed["stopReason"] == "stop"
        assert observed["sdk"] == {
            "keyFilename": expected_path,
            "apiKeyPresent": False,
            "project": "hobby-372711",
            "location": "global",
        }
    assert auth.read_bytes() == before
    assert "secret" not in result.stdout + result.stderr


# Break caught: setup missing new tier configs, replacing existing user edits
# under --if-absent, or clobbering the shared config and saved auth/settings.
def test_setup_seeds_vertex_tiers_and_preserves_existing_configs_on_rebuild(sandbox: tuple[Path, Path, Path]) -> None:
    home = sandbox[0]
    root = home / ".pi/agent"
    sentinels = {
        "auth.json": '{"synthetic":"unchanged"}',
        "settings.json": '{"theme":"custom"}',
        "extensions/subagent/config.json": '{"shared":"keep"}',
        "extensions/subagent.d/pigo/config.json": '{"custom":"keep"}',
    }
    for name, content in sentinels.items():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    for launcher in LAUNCHERS:
        (root / f"extensions/subagent.d/{launcher}/config.json").unlink()
    setup = DEVCONTAINER.parent / "scripts/setup_pi_superagents.zsh"
    for iteration in range(2):
        result = subprocess.run(
            ["zsh", str(setup), "--if-absent"],
            env=_env(sandbox),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        for launcher in LAUNCHERS:
            dest = root / f"extensions/subagent.d/{launcher}/config.json"
            assert dest.exists(), f"setup did not seed {launcher}"
            if iteration == 0:
                assert dest.read_bytes() == (DEVCONTAINER / f"pi-superagents/{launcher}.json").read_bytes()
                dest.write_text('{"custom":"keep"}')
            else:
                assert dest.read_text() == '{"custom":"keep"}'
        for name, content in sentinels.items():
            assert (root / name).read_text() == content
        assert not list(root.rglob("*.bak.*"))


# Break caught: inherited relative agent directory resolves against a child's
# different cwd and escapes the launcher's auth overlay.
def test_relative_agent_dir_stays_fixed_when_a_child_changes_cwd(sandbox: tuple[Path, Path, Path]) -> None:
    directory = sandbox[2] / "relative agent"
    directory.mkdir()
    (directory / "auth.json").write_text("{}")
    _real_namespace_probe(sandbox)
    result = _run(sandbox, "pigemf", extra={"PI_CODING_AGENT_DIR": "relative agent", "TEST_CHILD_CWD": "/"})
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    child = payload["child"]
    assert isinstance(child, dict)
    assert child["vertex_is_adc_marker"] is True
    assert child["native_key_is_adc_marker"] is True
