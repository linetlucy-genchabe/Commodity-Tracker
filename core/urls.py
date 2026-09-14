from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("facility/<int:facility_id>/", views.facility_drilldown, name="facility_drilldown"),
    path("upload/", views.upload_moh748, name="upload"),
    path("upload/<int:upload_id>/delete/", views.delete_moh748_upload, name="delete_upload"),
    path("chp/", views.chp_commodity_home, name="chp_home"),
    path("chp/upload/", views.upload_chp_commodity, name="chp_upload"),
    path("chp/upload/<int:upload_id>/delete/", views.delete_chp_commodity_upload, name="delete_chp_upload"),
]
