import re
import json
from typing import Any, List
from urllib.parse import unquote_plus

from django.http import HttpResponseBadRequest
from django.views.generic.list import BaseListView
from django.db.models.query import QuerySet
from django.conf import settings
from django.core.cache import cache
from django.contrib import messages

from .types import SourcedBibliographicItem
from .models import RefData
from .indexed import build_search_results
from .indexed import search_refs_relaton_struct
from .indexed import search_refs_relaton_field


websearch_re = [
    re.compile(r'"\S+"'),  # Quoted substring
    re.compile(r'\s-\S'),  # Token prepended with a - (negation)
    re.compile(r'\bOR\b'),  # OR operator
]
"""If any of these expression matches,
we will treat user-provided search string
as a PostgreSQL web search style query."""


class BaseCitationSearchView(BaseListView):
    """Generic view that handles citation search.

    Intended to be usable for both template-based GUI and API views."""

    # model = RefData
    paginate_by = 10

    limit_to = getattr(settings, 'DEFAULT_SEARCH_RESULT_LIMIT', 100)
    """Hard limit for found item count.

    If the user hits this limit, they are expected to provide
    a more precise query."""

    query_in_path = False
    """Whether query will appear as path component named ``query``
    (URLs must be configured appropriately).

    Otherwise, it’s expected to be supplied
    as a GET parameter named ``query``."""

    supported_query_formats = (
        'json_repr',
        'json_struct',
    )
    """Allowed values of query format in request."""

    query_format = None
    """Query format obtained from request,
    one of :attr:`supported_query_formats`."""

    query = None
    """Deserialized query, parsed from request."""

    show_all_by_default = False
    """Whether to show all items if query is not specified.

    Still subject to ``limit_to``."""

    result_cache_seconds = getattr(settings, 'SEARCH_CACHE_SECONDS', 3600)
    """How long to cache search results for. Results are cached as a list
    is constructed from query and query format. Default is one hour."""

    def get(self, request, *args, **kwargs):
        try:
            self.dispatch_parse_query(request, **kwargs)
        except UnsupportedQueryFormat:
            return HttpResponseBadRequest("Unsupported query format")
        except ValueError:
            return HttpResponseBadRequest("Unable to parse query")

        return super().get(request, *args, **kwargs)

    def paginate_queryset(self, queryset, page_size):
        try:
            return super().paginate_queryset(queryset, page_size)
        except Http404:
            if self.is_gui:
                messages.warning(
                    self.request,
                    "Requested page number doesn’t exist in this search, "
                    "or at least not anymore. Showing first page instead.")
                paginator = self.get_paginator(
                    queryset, page_size, orphans=self.get_paginate_orphans(),
                    allow_empty_first_page=True)
                page = paginator.page(1)
                return (
                    paginator,
                    page,
                    page.object_list,
                    page.has_other_pages(),
                )
            else:
                raise

    def get_queryset(self) -> List[SourcedBibliographicItem]:
        """Returns a ``QuerySet`` of ``RefData`` objects.

        If query is present, delegates to :meth:`dispatch_handle_query`,
        otherwise behavior depends on :attr:`show_all_by_default`."""

        if self.query is not None and self.query_format is not None:
            result_getter = (lambda: build_search_results(
                self.dispatch_handle_query(self.query)))

        else:
            if self.show_all_by_default:
                result_getter = (lambda: build_search_results(
                    RefData.objects.all()[:self.limit_to]))

            else:
                result_getter = (lambda: [])

        return cache.get_or_set(
            json.dumps({
                'query': self.query,
                'query_format': self.query_format,
                'limit': self.limit_to,
                'show_all': self.show_all_by_default,
            }),
            result_getter,
            self.result_cache_seconds)

    def get_context_data(self, **kwargs):
        """In addition to parent implementation,
        provides a ``query`` variable."""

        return dict(
            **super().get_context_data(**kwargs),
            result_cap=self.limit_to,
            query=self.query,
        )

    def dispatch_parse_query(self, request, **kwargs):
        """Parses query and sets up necessary instance attributes
        as a side effect. Guarantees :attr:`query` and :attr:`query_format`
        will be present.

        Delegates parsing to ``parse_{query-format}_query()`` method.

        Can throw exceptions due to bad input."""

        if not self.query_in_path:
            query = request.GET.get('query', None)
        else:
            query = unquote_plus(kwargs.get('query', '')).strip()

        query_format = request.GET.get('query_format', 'json_repr')

        if query:
            if query_format.lower() in self.supported_query_formats:
                parser = getattr(
                    self,
                    'parse_%s_query' % query_format.lower(),
                    self.parse_unsupported_query)
            else:
                parser = self.parse_unsupported_query

            self.query = parser(query)
            self.query_format = query_format

        else:
            self.query = None
            self.query_format = None

    def dispatch_handle_query(self, query) -> QuerySet[RefData]:
        """Handles query by delegating
        to ``handle_{query-format}_query()`` method.

        Is not expected to throw exceptions arising from bad input."""

        handler = getattr(self, 'handle_%s_query' % self.query_format)
        qs = handler(query)
        # print("got qs", [i.pk for i in qs])
        return qs

    def parse_unsupported_query(self, query_format: str, query: str):
        raise UnsupportedQueryFormat()

    def parse_json_repr_query(self, query: str) -> str:
        return query

    def handle_json_repr_query(self, query: str) -> QuerySet[RefData]:
        # Try to guess whether the websearch syntax was used.
        is_websearch = any(
            exp.search(query) is not None
            for exp in websearch_re)

        if not is_websearch:
            # If websearch, start with fast simple case-insensitive regex match
            # against select fields
            results = search_refs_relaton_field(
                {
                    'docid': '@.id like_regex %s' % json.dumps(f'(?i){query}'),
                },
                limit=self.limit_to,
                exact=True,
            )
            if len(results) > 0:
                return results

        # Otherwise, try the query (possibly websearch) against the entire body
        return search_refs_relaton_field({'': query}, limit=self.limit_to)

    def parse_json_struct_query(self, query: str) -> dict[str, Any]:
        try:
            struct = json.loads(query)
        except json.JSONDecodeError:
            raise ValueError("Invalid query format")
        else:
            return struct

    def handle_json_struct_query(
            self,
            query: dict[str, Any]) -> QuerySet[RefData]:
        return search_refs_relaton_struct(query, limit=self.limit_to)


class UnsupportedQueryFormat(ValueError):
    """Specified query format is not supported."""
    pass
