import inspect
from .storage import Store
from .metering.catalog import Catalog
from .metering.readings import Readings
from .metering.reconstruct import Reconstructor
from .metering.reconciliation import MeterBalance
from .allocation.rules import ProductionCalendar
from .allocation.engine import Allocator
from .settlement.tariffs import Tariffs
from .settlement.emissions import EmissionFactors
from .settlement.closing import Closing
from .settlement.demand import Demand
from .interfaces.bundles import Bundles
from .quality import Quality
from .values import object_fields, reject


class EnergyService:
    def __init__(self, path=":memory:"):
        self.store = Store(path)
        self.catalog = Catalog(self.store)
        self.readings = Readings(self.store, self.catalog)
        self.reconstruction = Reconstructor(self.store, self.catalog, self.readings)
        self.production = ProductionCalendar(self.store, self.catalog)
        self.allocation = Allocator(self.reconstruction, self.production)
        self.tariffs = Tariffs(self.store, self.catalog)
        self.emissions = EmissionFactors(self.store, self.catalog)
        self.closing = Closing(
            self.store, self.catalog, self.allocation, self.tariffs, self.emissions
        )
        self.bundles = Bundles(self.closing)
        self.balance = MeterBalance(self.catalog, self.reconstruction)
        self.demand = Demand(self.reconstruction)
        self.quality = Quality(
            self.store,
            self.catalog,
            self.readings,
            self.reconstruction,
            self.production,
            self.tariffs,
            self.emissions,
        )

    def actions(self):
        definitions = [
            (
                "catalog",
                self.catalog,
                [
                    "register",
                    "get",
                    "list",
                    "install",
                    "segment",
                    "segments",
                    "retire",
                    "replacement",
                    "coverage",
                    "inspect",
                ],
            ),
            (
                "reading",
                self.readings,
                [
                    "add",
                    "get",
                    "correct",
                    "history",
                    "segment",
                    "import_batch",
                    "parse",
                    "preview",
                ],
            ),
            (
                "reconstruction",
                self.reconstruction,
                [
                    "reconstruct",
                    "revision_impact",
                    "compare_revision",
                    "fleet",
                    "gaps",
                    "power_profile",
                ],
            ),
            (
                "production",
                self.production,
                [
                    "put",
                    "get",
                    "list",
                    "at",
                    "replace_span",
                    "import_shifts",
                    "coverage",
                    "products",
                ],
            ),
            (
                "allocation",
                self.allocation,
                [
                    "allocate",
                    "require_assigned",
                    "product_trace",
                    "compare",
                    "fleet",
                    "utilization",
                ],
            ),
            (
                "tariff",
                self.tariffs,
                [
                    "put",
                    "get",
                    "list",
                    "coverage",
                    "integrate",
                    "quote",
                    "revision_impact",
                    "daily_cost",
                ],
            ),
            (
                "emissions",
                self.emissions,
                ["put", "get", "list", "integrate", "intensity", "compare"],
            ),
            (
                "closing",
                self.closing,
                [
                    "preview",
                    "freeze",
                    "get",
                    "verify",
                    "current",
                    "effective",
                    "propose_correction",
                    "correction",
                    "approve_correction",
                    "drift",
                    "list",
                ],
            ),
            (
                "bundle",
                self.bundles,
                ["envelope", "export", "verify", "verify_content", "csv", "compare"],
            ),
            (
                "balance",
                self.balance,
                ["compare", "tree", "allocate_residual", "compare_snapshots"],
            ),
            (
                "demand",
                self.demand,
                ["windows", "rolling_peak", "charge", "exceedances", "load_factor"],
            ),
            (
                "quality",
                self.quality,
                [
                    "inspect",
                    "readiness",
                    "staleness",
                    "revision_summary",
                    "conservation",
                    "system",
                ],
            ),
            (
                "storage",
                self.store,
                ["verify_audit", "audit_page", "integrity", "backup"],
            ),
        ]
        result = {}
        for prefix, target, methods in definitions:
            for method in methods:
                result[prefix + "." + method] = getattr(target, method)
        return result

    def dispatch(self, request):
        object_fields(request, {"action", "arguments"}, {"action"})
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            reject("input", "arguments must be an object")
        action = request["action"]
        actions = self.actions()
        if not isinstance(action, str) or action not in actions:
            reject("action", "unknown operation")
        method = actions[action]
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as exc:
            reject("input", "operation arguments differ", reason=str(exc))
        return method(**arguments)

    def describe(self):
        result = []
        for name, method in sorted(self.actions().items()):
            parameters = []
            for parameter in inspect.signature(method).parameters.values():
                entry = {
                    "name": parameter.name,
                    "required": parameter.default is inspect.Parameter.empty,
                }
                if parameter.default is not inspect.Parameter.empty:
                    entry["default"] = parameter.default
                parameters.append(entry)
            result.append({"action": name, "parameters": parameters})
        return result

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
