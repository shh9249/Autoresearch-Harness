#include <iostream>
#include <vector>
#include <algorithm>
int main(){
  int n,m; if(!(std::cin>>n>>m)) return 1;
  std::vector<std::vector<int>> s(m);
  for(auto& a:s) for(int j=0;j<n/m;j++){int x;std::cin>>x;a.push_back(x);}
  for(int v=1;v<=n;v++){
    int i=0; while(std::find(s[i].begin(),s[i].end(),v)==s[i].end())i++;
    int j=std::find(s[i].begin(),s[i].end(),v)-s[i].begin();
    if(j+1<(int)s[i].size()){
      int t=(i+1)%m; std::cout<<s[i][j+1]<<" "<<t+1<<"\n";
      s[t].insert(s[t].end(),s[i].begin()+j+1,s[i].end());s[i].resize(j+1);
    }
    std::cout<<v<<" 0\n";s[i].pop_back();
  }
}
