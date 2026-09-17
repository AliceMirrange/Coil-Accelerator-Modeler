from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
MODEL_GUI = (ROOT / "maxwell_accelerator_builder.pyw").read_text(encoding="utf-8")
CIRCUIT_GUI = (ROOT / "maxwell_circuit_toolkit" / "gui.py").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_is_bilingual_and_names_the_click_sequence():
    assert "## 中文使用说明" in README
    assert "## English Instructions" in README
    for button in (
        "刷新参数表",
        "建立 Maxwell2D 模型",
        "添加 Optimization Setup",
        "生成并回写 External Circuit",
        "同步 Circuit 全局变量 → Parameter Values",
    ):
        assert button in README


def test_validation_is_merged_into_refresh_button():
    assert "validate_button" not in MODEL_GUI
    assert "_validate_only" not in MODEL_GUI
    assert "command=self._refresh_and_validate" in MODEL_GUI


def test_long_gui_descriptions_were_moved_to_readme():
    gui_source = MODEL_GUI + CIRCUIT_GUI
    for removed_text in (
        "未勾选时：完成后仅释放 PyAEDT 控制",
        "磁阻：Fn=C(n-1)",
        "几何含义：输入 Di/Do",
        "建模流程：Create External Circuit",
        "图形模式会优先复用已有 AEDT 会话",
    ):
        assert removed_text not in gui_source
    for moved_topic in (
        "PyAEDT 控制",
        "Fn=C(n-1)+u_n*(Cn-C(n-1))",
        "径向宽度为 `(Do-Di)/2`",
        "Create External Circuit",
        "图形模式会优先复用已有 AEDT 会话",
    ):
        assert moved_topic in README


def test_application_and_test_runner_disable_cache_generation():
    runner = (ROOT / "run_tests.cmd").read_text(encoding="ascii")
    assert "sys.dont_write_bytecode = True" in MODEL_GUI
    assert 'addopts = "-p no:cacheprovider"' in PYPROJECT
    assert "set PYTHONDONTWRITEBYTECODE=1" in runner


def test_readme_documents_projectile_offset_and_simulated_scr_topology():
    assert "薄膜电容+模拟SCR" in README
    assert "$POSon_n-$proj_initZ" in README
    assert "Moving Vector为(0,0,$proj_initZ)" in README
    assert "0,0,$proj_initZ" in README
    assert "`Pw=1e9mm`" in README


def test_readme_documents_unitless_real_si_variable_contract():
    assert "所有项目变量均不带单位" in README
    assert "虚部为 0" in README
    assert "unitless and stores a real SI value" in README
