import pytest

from mcctl.props import (
    PropError,
    PropertiesFile,
    get_var,
    parse_heap,
    props_diff,
    set_heap,
    set_var,
    size_to_bytes,
    validate_prop,
)

SAMPLE = """\
#Minecraft server properties
#Mon May 25 12:00:00 UTC 2026
enable-rcon=false
server-ip=0.0.0.0
view-distance=10
motd=CarborioLand \\u00a7aMMC5
use-native-transport=false
"""

VARIABLES = """\
# ServerPackCreator variables
MINECRAFT_VERSION=1.21.1
MODLOADER=NeoForge
MODLOADER_VERSION=21.1.228
JAVA="/opt/graalvm/bin/java"
JAVA_ARGS="-Xms12G -Xmx12G -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 -XX:+ExplicitGCInvokesConcurrent -Djava.net.preferIPv4Stack=true"
SKIP_JAVA_CHECK=true
WAIT_FOR_USER_INPUT=false
USE_SSJ=true
"""


def test_parse_preserves_comments_and_order():
    pf = PropertiesFile.parse(SAMPLE)
    assert pf.get("server-ip") == "0.0.0.0"
    out = pf.render()
    assert out.startswith("#Minecraft server properties\n#Mon May 25")
    assert out.splitlines()[2] == "enable-rcon=false"


def test_set_existing_and_new():
    pf = PropertiesFile.parse(SAMPLE)
    pf.set("view-distance", "12")
    pf.set("simulation-distance", "8")
    text = pf.render()
    assert "view-distance=12" in text
    assert text.rstrip().endswith("simulation-distance=8")
    assert text.count("view-distance=") == 1


@pytest.mark.parametrize("key,value,expect", [
    ("pvp", "ON", "true"),
    ("pvp", "0", "false"),
    ("difficulty", "HARD", "hard"),
    ("view-distance", "32", "32"),
    ("max-tick-time", "-1", "-1"),
])
def test_validate_ok(key, value, expect):
    assert validate_prop(key, value) == expect


@pytest.mark.parametrize("key,value", [
    ("pvp", "maybe"),
    ("view-distance", "2"),
    ("view-distance", "33"),
    ("view-distance", "ten"),
    ("difficulty", "impossible"),
])
def test_validate_rejects(key, value):
    with pytest.raises(PropError):
        validate_prop(key, value)


def test_diff_masks_rcon_password():
    a = PropertiesFile.parse("rcon.password=old\nmotd=x\n")
    b = PropertiesFile.parse("rcon.password=new\nmotd=y\n")
    lines = "\n".join(props_diff(a, b))
    assert "old" not in lines and "new" not in lines
    assert "motd: x -> y" in lines


def test_get_set_var():
    assert get_var(VARIABLES, "JAVA") == "/opt/graalvm/bin/java"
    assert get_var(VARIABLES, "SKIP_JAVA_CHECK") == "true"
    assert get_var(VARIABLES, "NOPE") is None
    out = set_var(VARIABLES, "WAIT_FOR_USER_INPUT", "false")
    assert out.count("WAIT_FOR_USER_INPUT") == 1
    out = set_var(VARIABLES, "NEW_KEY", "v")
    assert 'NEW_KEY="v"' in out


def test_parse_heap():
    assert parse_heap(get_var(VARIABLES, "JAVA_ARGS")) == ("12G", "12G")
    assert parse_heap("-Xmx8192M nothing") == (None, "8192M")


def test_set_heap_preserves_flags():
    out = set_heap(VARIABLES, "14G")
    args = get_var(out, "JAVA_ARGS")
    assert parse_heap(args) == ("14G", "14G")
    # Aikar's & friends survive untouched
    for flag in ("-XX:+UseG1GC", "-XX:+ExplicitGCInvokesConcurrent",
                 "-Djava.net.preferIPv4Stack=true", "-XX:MaxGCPauseMillis=200"):
        assert flag in args
    assert get_var(out, "JAVA") == "/opt/graalvm/bin/java"


def test_set_heap_adds_when_missing():
    vars_no_heap = 'JAVA_ARGS="-XX:+UseG1GC"\n'
    args = get_var(set_heap(vars_no_heap, "8G"), "JAVA_ARGS")
    assert parse_heap(args) == ("8G", "8G")
    assert "-XX:+UseG1GC" in args


def test_size_to_bytes():
    assert size_to_bytes("12G") == 12 * 1024**3
    assert size_to_bytes("512m") == 512 * 1024**2
    with pytest.raises(PropError):
        size_to_bytes("12GB")
    with pytest.raises(PropError):
        size_to_bytes("lots")


def test_set_heap_requires_java_args():
    with pytest.raises(PropError, match="JAVA_ARGS"):
        set_heap("FOO=bar\n", "8G")
