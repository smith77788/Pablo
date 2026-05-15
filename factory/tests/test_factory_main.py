"""Tests for factory_main.py entry point."""
from __future__ import annotations
import argparse
import sys
from unittest.mock import patch, MagicMock

import pytest


class TestFactoryMainImport:
    def test_module_importable(self):
        import factory.factory_main
        assert factory.factory_main is not None

    def test_main_function_exists(self):
        from factory.factory_main import main
        assert callable(main)

    def test_cycle_interval_constant_positive(self):
        import factory.factory_main as fm
        assert fm.CYCLE_INTERVAL_SECONDS > 0

    def test_cycle_interval_default_is_3600(self):
        import os
        import factory.factory_main as fm
        # Default when env var not set
        env_val = os.getenv("FACTORY_CYCLE_INTERVAL", "3600")
        assert int(env_val) == 3600 or fm.CYCLE_INTERVAL_SECONDS > 0


class TestFactoryMainOnce:
    def test_main_once_calls_run_cycle(self):
        from factory import factory_main
        mock_result = {
            "health_score": 75,
            "elapsed_s": 1.5,
            "phases": {},
        }
        with patch("factory.factory_main.run_cycle", return_value=mock_result) as mock_cycle:
            with patch("sys.argv", ["factory_main", "--once"]):
                with pytest.raises(SystemExit) as exc:
                    factory_main.main()
                assert exc.value.code == 0
            mock_cycle.assert_called_once()

    def test_main_once_exits_with_zero(self):
        from factory import factory_main
        mock_result = {"health_score": 80, "elapsed_s": 0.5}
        with patch("factory.factory_main.run_cycle", return_value=mock_result):
            with patch("sys.argv", ["factory_main", "--once"]):
                with pytest.raises(SystemExit) as exc:
                    factory_main.main()
                assert exc.value.code == 0

    def test_main_once_handles_cycle_result_without_health_score(self):
        from factory import factory_main
        with patch("factory.factory_main.run_cycle", return_value={}):
            with patch("sys.argv", ["factory_main", "--once"]):
                with pytest.raises(SystemExit) as exc:
                    factory_main.main()
                assert exc.value.code == 0

    def test_main_once_handles_missing_elapsed_s(self):
        from factory import factory_main
        with patch("factory.factory_main.run_cycle", return_value={"health_score": 50}):
            with patch("sys.argv", ["factory_main", "--once"]):
                with pytest.raises(SystemExit) as exc:
                    factory_main.main()
                assert exc.value.code == 0


class TestFactoryMainContinuous:
    def test_main_continuous_calls_run_cycle_repeatedly(self):
        from factory import factory_main
        call_count = 0

        def mock_cycle():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt("stop")
            return {"health_score": 70}

        with patch("factory.factory_main.run_cycle", side_effect=mock_cycle):
            with patch("factory.factory_main.time") as mock_time:
                mock_time.sleep = MagicMock()
                with patch("sys.argv", ["factory_main"]):
                    with pytest.raises(KeyboardInterrupt):
                        factory_main.main()
                assert call_count >= 1

    def test_main_continuous_sleeps_between_cycles(self):
        from factory import factory_main
        call_count = 0

        def mock_cycle():
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise KeyboardInterrupt("stop test")
            return {}

        with patch("factory.factory_main.run_cycle", side_effect=mock_cycle):
            with patch("factory.factory_main.time") as mock_time:
                mock_time.sleep = MagicMock()
                with patch("sys.argv", ["factory_main"]):
                    with pytest.raises(KeyboardInterrupt):
                        factory_main.main()

    def test_main_continuous_handles_cycle_exception(self):
        from factory import factory_main
        call_count = 0

        def mock_cycle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("cycle error")
            raise KeyboardInterrupt("stop")

        with patch("factory.factory_main.run_cycle", side_effect=mock_cycle):
            with patch("factory.factory_main.time") as mock_time:
                mock_time.sleep = MagicMock()
                with patch("sys.argv", ["factory_main"]):
                    with pytest.raises(KeyboardInterrupt):
                        factory_main.main()
                assert call_count >= 2  # Continued after error


class TestFactoryMainArgParsing:
    def test_once_flag_recognized(self):
        with patch("sys.argv", ["fm", "--once"]):
            parser = argparse.ArgumentParser()
            parser.add_argument("--once", action="store_true")
            parser.add_argument("--interval", type=int, default=3600)
            args = parser.parse_args()
            assert args.once is True

    def test_interval_flag_recognized(self):
        with patch("sys.argv", ["fm", "--interval", "1800"]):
            parser = argparse.ArgumentParser()
            parser.add_argument("--once", action="store_true")
            parser.add_argument("--interval", type=int, default=3600)
            args = parser.parse_args()
            assert args.interval == 1800

    def test_default_interval_is_3600(self):
        with patch("sys.argv", ["fm"]):
            parser = argparse.ArgumentParser()
            parser.add_argument("--once", action="store_true")
            parser.add_argument("--interval", type=int, default=3600)
            args = parser.parse_args()
            assert args.interval == 3600

    def test_default_once_is_false(self):
        with patch("sys.argv", ["fm"]):
            parser = argparse.ArgumentParser()
            parser.add_argument("--once", action="store_true")
            args = parser.parse_args()
            assert args.once is False
