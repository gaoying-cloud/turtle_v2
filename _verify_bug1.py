"""Wrapper: run BUG-1 test, capture output to file."""
import sys, os, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

result_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_bug1_result.txt")

try:
    from tests.test_gen_report import TestGenerateParamsSection
    t = TestGenerateParamsSection()

    results = []
    for name in ["test_with_mock_data", "test_no_grid_dir"]:
        method = getattr(t, name)
        try:
            method()
            results.append(f"  {name} ... PASSED")
        except Exception as e:
            results.append(f"  {name} ... FAILED\n{traceback.format_exc()}")

    with open(result_path, "w", encoding="utf-8") as f:
        f.write("\n".join(results))
    print("Done, see _test_bug1_result.txt")
except Exception as e:
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(f"FATAL: {traceback.format_exc()}")
    print("Error, see _test_bug1_result.txt")
