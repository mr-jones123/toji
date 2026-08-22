from pyapp.worker import run


class Pipeline:
    """Top-level pipeline."""

    def execute(self):
        return run()


def main():
    p = Pipeline()
    p.execute()
    return run()
