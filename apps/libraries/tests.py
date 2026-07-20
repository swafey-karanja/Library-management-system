from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from .models import Library


class LibraryEndpointTests(APITestCase):
    """
    Tests for the Library create and list endpoints.

    We use APITestCase (from rest_framework.test) instead of Django's
    plain TestCase because it gives us `self.client` pre-configured to
    send requests and parse DRF responses conveniently, plus helpful
    assertion support for status codes like status.HTTP_201_CREATED.

    IMPORTANT NOTE ABOUT managed = False:
    Our Library model has `managed = False`, meaning Django's migration
    system never creates this table — it assumes the table already exists
    in the database (which it does, in your real Postgres database, via
    the raw SQL script). However, Django's test runner creates a SEPARATE,
    temporary test database for running tests, and since managed = False
    tables are skipped during migrations, this 'library' table will NOT
    automatically exist in that test database.

    This means these tests will fail with a "relation library does not
    exist" error unless one of the following is true:
        1. You have a migration (even an empty/manual one) that explicitly
           runs the same CREATE TABLE SQL for 'library' as part of the
           Django migration history, OR
        2. You configure Django's test settings to reuse your real
           development database schema rather than building a fresh one
           (less ideal, since tests could affect real data), OR
        3. You use Django's `migrate --run-syncdb` style approach or a
           custom test database setup that runs your raw SQL schema
           script before tests start.

    For now, this test file is written assuming option 1: that a
    migration exists which creates the 'library' table structure (even
    though Django won't manage further changes to it). We'll need to
    address this when we get to the migrations step, if we haven't
    already — flag this if your tests fail with a relation/table error.
    """

    def setUp(self):
        """
        setUp() runs automatically before every individual test method
        below. We use it here to create one known Library record in the
        (test) database, which the list-endpoint test can check against.
        """
        # Bypass JWT authentication for all requests made by this test
        # client. force_authenticate() tells DRF to skip the auth check
        # entirely, so we can test the view logic in isolation without
        # needing to generate real tokens in every test.
        self.client.force_authenticate(user=None)
        self.existing_library = Library.objects.create(
            name="Existing Test Library",
            url="https://existing-test-library.example.com",
            primary_color="#123456",
            secondary_color="#abcdef",
            enabled_modules={"members": True, "fines": False},
        )

    def test_create_library_success(self):
        """
        Sends a valid POST request to the create-library endpoint and
        checks that:
            1. The response status code is 201 Created.
            2. The response body contains the data we sent.
            3. A library_id was generated automatically (not supplied by us).
            4. The Library row actually exists in the database afterwards.
        """
        url = reverse("create-library")

        payload = {
            "name": "New Test Library",
            "url": "https://new-test-library.example.com",
            "primary_color": "#ff0000",
            "secondary_color": "#00ff00",
            "enabled_modules": {"members": True, "books": True},
        }

        response = self.client.post(url, payload, format="json")

        # Check the HTTP status code reflects successful creation.
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check the response echoes back the data we sent.
        self.assertEqual(response.data["name"], payload["name"])
        self.assertEqual(response.data["url"], payload["url"])
        self.assertEqual(response.data["enabled_modules"], payload["enabled_modules"])

        # Check that a library_id was generated for us automatically,
        # confirming our serializer's create() override worked.
        self.assertIn("library_id", response.data)
        self.assertIsNotNone(response.data["library_id"])

        # Confirm the row genuinely exists in the database, not just in
        # the API response — querying the model directly here.
        self.assertTrue(Library.objects.filter(name="New Test Library").exists())

    def test_create_library_missing_required_field(self):
        """
        Sends a POST request missing the required 'name' field, and
        checks that the API correctly rejects it with a 400 Bad Request
        and a helpful error message, rather than creating a broken row.
        """
        url = reverse("create-library")

        payload = {
            "url": "https://no-name-library.example.com",
        }

        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # DRF returns validation errors keyed by field name.
        self.assertIn("name", response.data)

    def test_create_library_duplicate_name_rejected(self):
        """
        Confirms the database/model-level uniqueness constraint on `name`
        is enforced through the API — attempting to create a second
        Library with the same name as one that already exists (created in
        setUp) should fail validation rather than succeed.
        """
        url = reverse("create-library")

        payload = {
            "name": self.existing_library.name,  # duplicate on purpose
            "url": "https://different-url.example.com",
        }

        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_list_libraries_success(self):
        """
        Sends a GET request to the list-libraries endpoint and checks
        that:
            1. The response status code is 200 OK.
            2. The library created in setUp() appears in the results.
        """
        url = reverse("list-libraries")

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Collect just the names returned, to check our setUp library
        # is present without depending on exact list ordering.
        returned_names = [item["name"] for item in response.data]
        self.assertIn(self.existing_library.name, returned_names)

    def test_list_libraries_reflects_newly_created_library(self):
        """
        Creates an additional Library directly via the ORM, then checks
        that the list endpoint reflects it — confirming the list view
        queries the database live rather than returning cached/stale data.
        """
        Library.objects.create(
            name="Another Library For Listing",
            url="https://another-library.example.com",
        )

        url = reverse("list-libraries")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_names = [item["name"] for item in response.data]
        self.assertIn("Another Library For Listing", returned_names)
        # We expect at least 2 libraries now: the one from setUp() and
        # the one just created above.
        self.assertGreaterEqual(len(response.data), 2)
