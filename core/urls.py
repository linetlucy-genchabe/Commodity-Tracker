from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("facility/<int:facility_id>/", views.facility_drilldown, name="facility_drilldown"),
    path("upload/", views.upload_moh748, name="upload"),
]
