
from rest_framework.generics import ListAPIView
from .models import ModelRun
from .serializers import ModelRunSerializer

class ModelRunListView(ListAPIView):
    serializer_class = ModelRunSerializer

    def get_queryset(self):
        queryset = ModelRun.objects.all().order_by('-id')
        model_run_completed = self.request.query_params.get('model_run_completed')
        if model_run_completed is not None:
            if model_run_completed.lower() in ['true', '1', 'yes']:
                queryset = queryset.filter(model_run_completed=True)
            elif model_run_completed.lower() in ['false', '0', 'no']:
                queryset = queryset.filter(model_run_completed=False)
        status = self.request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        return queryset
