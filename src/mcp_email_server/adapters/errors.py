class AdapterConfigurationError(RuntimeError):
    def __init__(self, missing_configuration: list[str]):
        self.missing_configuration = missing_configuration
        message = "Missing required configuration: " + ", ".join(missing_configuration)
        super().__init__(message)


class AdapterExecutionError(RuntimeError):
    pass
