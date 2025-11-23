from django.urls import path
from . import views

urlpatterns = [
    # --- API ESP ---
    path('api/status/', views.get_status_api, name='get_status_api'),
    path('api/sensor-data/', views.sensor_data_api, name='sensor_data_api'),
    path('api/manual-left/', views.manual_left_api, name='manual_left_api'),
    path('api/manual-right/', views.manual_right_api, name='manual_right_api'),
    path('api/manual-control-esp/', views.manual_control_esp_api, name='manual_control_esp_api'),

    path('api/set-params/', views.set_parameters_api, name='set_parameters_api'),
    path('api/toggle-automatic/', views.toggle_automatic_mode, name='toggle_automatic_mode'),

    # --- Páginas frontend ---
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('historico/', views.historico, name='historico'),
]
