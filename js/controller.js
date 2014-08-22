app.controller('ChartController', function($scope, $http) {
    // Get the parameters to pass down to the api call.
    var search = {};
    if (window.location.search.length > 0) {
      var s = window.location.search.substr(1);
      var pieces = s.split('&');
      for (var i in pieces) {
        var kv = pieces[i].split('=');
        search[kv[0]] = kv[1];
      }
    }
    if (!search['samples']) {
      search['samples'] = 200;
    }
    var paramlist = ['?'];
    for (var k in search) {
      paramlist.push(encodeURIComponent(k) + '=' + encodeURIComponent(search[k]));
    }
    $http.get('/api/chartdata?' + paramlist.join('&')).success(function(data, status) {
      if (status != 200) {
        document.getElementById('chart_div').innerHTML = "Error loading graph: " + status;
        return;
      }
      var chart_div = document.getElementById('chart_div');
      var rows = data.data.rows;
      var table = new google.visualization.DataTable();
      table.addColumn('date', 'Date')
      table.addColumn('number', 'Trend')
      table.addColumn({id: 'wtpair', type: 'number', role: 'interval'})
      table.addColumn({id: 'wtpair', type: 'number', role: 'interval'})
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        // Parse the date
        pieces = row[0].split(/\D/); // non-digits
        var d = new Date(pieces[0], pieces[1] - 1, pieces[2]);
        table.addRow([d, row[2], row[2], row[1]]);
      }
      var chart = new google.visualization.LineChart(chart_div);
      var options = {
        "title": "",
        "lineWidth": 1,
        "chartArea": {"left": "15%", "width": "80%", "top": "5%", "height": "75%"},
        "hAxis": {"gridlines": {"color": "#eee"},
                  "textStyle": {"fontSize": 10}},
        "vAxis": {"gridlines": {"color": "#eee"},
                  "textStyle": {"fontSize": 11}},
        "width": chart_div.width,
        "height": chart_div.height,
        "intervals": {'style': 'bars', 'barWidth': 0, 'lineWidth': 0.7, 'pointSize': 1.5, "fillOpacity": 0.3, "color": "grey"},
        "legend": "none",
      };
      chart.draw(table, options);
    });
});
