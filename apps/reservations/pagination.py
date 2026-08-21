from rest_framework.pagination import PageNumberPagination


class ReservationPagination(PageNumberPagination):
    """
    Controls how the reservations list endpoint paginates results.

    - `page_size`: default number of results per page.
    - `page_size_query_param`: lets the client override the page size,
      e.g. GET /api/reservations/?page_size=50
    - `max_page_size`: hard ceiling so a client can't request an
      unreasonably large page (protects the DB/server from abuse).

    Response shape this produces:
        {
            "count": 123,
            "next": "http://.../api/reservations/?page=3",
            "previous": "http://.../api/reservations/?page=1",
            "results": [ ...serialized reservations... ]
        }
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100