from django.urls import path
from . import views
from .views import manual_control_esp_api


urlpatterns = [
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('api/status/', views.get_status_api, name='api_status'),
    path('api/set-parameters/', views.set_parameters_api, name='api_set_parameters'),
    path('api/manual-control/', views.manual_control_api, name='api_manual_control'),
    path('api/sensor/', views.sensor_data_api, name='api_sensor_data'),
    path('historico/', views.historico, name='historico'),
    path('api/toggle_automatic_mode/', views.toggle_automatic_mode, name='toggle_automatic_mode'),
    path("api/manual-control-esp/", manual_control_esp_api),
]
