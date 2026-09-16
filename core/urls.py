from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("facility/<int:facility_id>/", views.facility_drilldown, name="facility_drilldown"),
    path("upload/", views.upload_moh748, name="upload"),
    path("upload/<int:upload_id>/delete/", views.delete_moh748_upload, name="delete_upload"),
    path("chp/", views.chp_commodity_home, name="chp_home"),
    path("chp/moh748/", views.chp_moh748_page, name="chp_moh748"),
    path("chp/upload/", views.upload_chp_commodity, name="chp_upload"),
    path("chp/upload/<int:upload_id>/delete/", views.delete_chp_commodity_upload, name="delete_chp_upload"),
    path("chp/export/balances.csv", views.export_chp_balances_csv, name="chp_export_balances"),
    path("chp/export/moh748.csv", views.export_chp_moh748_csv, name="chp_export_moh748"),
    path("chp/export/stock-status.csv", views.export_chp_stock_status_csv, name="chp_export_stock_status"),
]
