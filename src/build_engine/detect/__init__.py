"""Framework and package-manager detection package."""

from build_engine.detect.compatibility import CompatibilityResult, Guidance
from build_engine.detect.framework import (
    BuildPlan,
    DetectionError,
    FrameworkDetection,
    FrameworkProfile,
    NodeSelection,
    detect_framework,
    infer_generic_output,
    plan_build,
    select_node_version,
)
from build_engine.detect.lockfiles import (
    PackageManagerDetection,
    detect_package_manager,
    install_command,
    run_script_command,
)
from build_engine.detect.package_json import PackageJson, load_package_json

__all__ = [
    "BuildPlan",
    "CompatibilityResult",
    "DetectionError",
    "FrameworkDetection",
    "FrameworkProfile",
    "Guidance",
    "NodeSelection",
    "PackageJson",
    "PackageManagerDetection",
    "detect_framework",
    "detect_package_manager",
    "infer_generic_output",
    "install_command",
    "load_package_json",
    "plan_build",
    "run_script_command",
    "select_node_version",
]
