"""Точка входа в изолированном интерпретаторе. Печатает только служебный результат unittest."""
import contextlib
import json
import os
import re
import sys
import unittest


class CheckResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.first = ''
        self.problem = ''

    def startTest(self, test):
        super().startTest(test)
        if not self.first:
            self.first = test.id()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.problem = self.problem or test.id()

    def addError(self, test, err):
        super().addError(test, err)
        self.problem = self.problem or test.id()


def run():
    source, assigned, *names = sys.argv[1:]
    # sys.path начинается с неизменяемой stdlib. Разрешаем импорт только явно переданной копии проекта.
    sys.path.append(source)
    sys.dont_write_bytecode = True
    # Тест видит корень снимка через __file__; рабочий каталог процесса оставляем в scratch.
    candidate = os.path.normcase(os.path.join(source, assigned))
    observed = False

    def observe(event, args):
        nonlocal observed
        if event == 'exec':
            observed |= os.path.normcase(os.path.abspath(args[0].co_filename)) == candidate
        elif event == 'open' and isinstance(args[0], str):
            observed |= os.path.normcase(os.path.abspath(args[0])) == candidate

    sys.addaudithook(observe)
    loader = unittest.TestLoader()
    if names[:1] == ['discover']:
        suite = loader.discover(source)
    else:
        suite = loader.loadTestsFromNames(names)
    result = CheckResult()
    suite.run(result)
    identifier = str(result.problem or result.first)
    safe_name = identifier[:220] if re.fullmatch(r'[\w.]+', identifier) else ''
    return dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
                unexpected_successes=len(result.unexpectedSuccesses), load_errors=bool(loader.errors),
                test_name=safe_name, observed=observed)


if __name__ == '__main__':
    with open(os.devnull, 'w', encoding='utf-8') as muted:
        with contextlib.redirect_stdout(muted), contextlib.redirect_stderr(muted):
            outcome = run()
    print(json.dumps(outcome))
