from django.urls import include, path

urlpatterns = [
    path("garmin/", include("garmin.urls")),
]
