from __future__ import annotations

from fastapi import APIRouter

from app.ui import (
    expenses,
    manual,
    merge_split,
    places,
    reports,
    review,
    settings,
    stats,
    trips,
)

# Re-exports: these names were importable straight from `app.ui` before this
# package split (production code and tests import several of them directly,
# e.g. app/email_digest.py's `_fetch_range_trips_in`), so every module keeps
# its old top-level name reachable here even though the implementation now
# lives one level down.
from app.ui._common import (  # noqa: F401
    CATEGORIES,
    EXCLUSIONS,
    EXCLUSION_LABELS,
    EXPORT_MEDIA_TYPES,
    RULE_CATEGORIES,
    TRIP_COLUMNS,
    VEHICLE_FILTER_UNASSIGNED,
    _END_ADDRESS_SQL,
    _END_PLACE_NAME_SQL,
    _START_ADDRESS_SQL,
    _START_PLACE_NAME_SQL,
    _escape_ilike_term,
    _fetch_recent_purposes,
    _fetch_trip,
    _month_bounds,
    _month_page_url,
    _parse_range_query_dates,
    _parse_vehicle_form,
    _parse_vehicle_id,
    _path_distance_m,
    _poke_snap_worker,
    _redirect_back,
    _trip_filter_sql,
    _url_with_filters,
    parse_date_range,
)
from app.ui.expenses import _EXPENSE_SELECT_JOIN, _parse_expense_input  # noqa: F401
from app.ui.manual import (  # noqa: F401
    MANUAL_ROUTE_UNAVAILABLE_NOTICE,
    ManualTripValidationError,
    _ManualRouteEndpoints,
    _local_time_is_real,
    _parse_manual_route_coord,
    _resolve_manual_route_endpoints,
    _resolve_missing_trip_osrm_hint,
    parse_manual_trip_input,
)
from app.ui.merge_split import _validate_split_distance  # noqa: F401
from app.ui.places import (  # noqa: F401
    _fetch_boundary_overrides_rows,
    _fetch_places_rows,
    _fetch_rules_rows,
    _side_desc,
)
from app.ui.reports import (  # noqa: F401
    _AnnualReportData,
    _RangeReportData,
    _build_annual_report_data,
    _build_range_report_data,
    _env_override_years,
    _fetch_range_trips,
    _fetch_range_trips_in,
    _fetch_year_expense_report,
    _fetch_year_odometer_coverage,
    _multiyear_window,
)
from app.ui.review import (  # noqa: F401
    _fetch_review_card,
    _render_review_card,
    _review_filter_sql,
    _review_url,
)
from app.ui.settings import (  # noqa: F401
    _fetch_device_fixes,
    _fetch_odometer_context,
    _fetch_rates_rows,
    _render_odometer_table,
    _render_vehicles_table,
)
from app.ui.trips import (  # noqa: F401
    _apply_human_tag,
    _delete_trip_in,
    _fetch_month_page,
    _fetch_trip_card_context,
    _trip_edit_values,
    _trip_position,
)


def make_router() -> APIRouter:
    router = APIRouter()
    # Registration order is load-bearing: Starlette matches on regex shape
    # before FastAPI validates path types, so a bare {year} or {trip_id}
    # segment will swallow a literal sibling registered after it. This is
    # the same order the routes were defined in when this package was still
    # a single module; several modules register in more than one place here
    # because their routes were interleaved with other domains' routes in
    # that original order.
    stats.register(router)
    trips.register_archive(router)
    review.register(router)
    trips.register_month_page(router)
    reports.register(router)
    expenses.register(router)
    manual.register(router)
    trips.register(router)
    trips.register_delete(router)
    merge_split.register(router)
    trips.register_batch_and_points(router)
    merge_split.register_split(router)
    places.register_boundary_override(router)
    settings.register(router)
    places.register(router)
    return router
